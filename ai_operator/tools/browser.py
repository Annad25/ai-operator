"""Browser control through Playwright.

The model never sees pixels. Each action returns a text snapshot: page text plus
a list of interactive elements, each with a short ref (e1, e2...) the model uses
to act. This is the same idea as Playwright MCP's accessibility snapshots:
cheap in tokens, works with non-vision models, and the agent targets an exact
element instead of guessing coordinates.

Refs are only valid for one snapshot. Learned procedures therefore store
elements by (role, accessible name), which survives re-renders.
"""
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

from .. import config

SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-ref]').forEach(e => e.removeAttribute('data-ref'));
  const sel = 'a,button,input,select,textarea,[role=button],[role=link]';
  const out = []; let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (el.type === 'hidden' || !(el.offsetWidth || el.offsetHeight)) continue;
    const ref = 'e' + (++i); el.setAttribute('data-ref', ref);
    const tag = el.tagName.toLowerCase();
    let name = el.getAttribute('aria-label') || '';
    if (!name && el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) name = l.innerText; }
    if (!name) name = el.placeholder || (tag === 'select' ? '' : el.innerText) || el.value || el.name || '';
    let role = el.getAttribute('role') || {a:'link', button:'button', select:'combobox', textarea:'textbox'}[tag];
    if (!role && tag === 'input') role = ['submit','button'].includes(el.type) ? 'button' : (el.type === 'checkbox' ? 'checkbox' : 'textbox');
    const item = {ref, role, name: name.trim().replace(/\s+/g,' ').slice(0, 80)};
    if (tag === 'select') { item.options = [...el.options].map(o => o.text); item.value = el.selectedOptions[0]?.text || ''; }
    else if (['input','textarea'].includes(tag) && !['submit','button'].includes(el.type)) { item.value = el.value; if (el.type !== 'text') item.type = el.type; }
    if (tag === 'a') item.href = el.getAttribute('href');
    out.push(item);
  }
  return {url: location.href, title: document.title, text: document.body.innerText.slice(0, 1800), elements: out};
}
"""


class Browser:
    def __init__(self):
        self._pw = None
        self.page = None
        self.last = {"url": "", "elements": []}
        self.last_status = None

    def _ensure(self):
        if self.page is None:
            self._pw = sync_playwright().start()
            b = self._pw.chromium.launch(headless=not config.HEADED, slow_mo=250 if config.HEADED else 0)
            self.page = b.new_page(viewport={"width": 1100, "height": 800})
            self.page.on("response", self._on_response)

    def _on_response(self, resp):
        if resp.request.resource_type == "document" and resp.frame == self.page.main_frame:
            self.last_status = resp.status

    def close(self):
        if self._pw:
            self._pw.stop()
            self._pw, self.page = None, None

    # ---- observation ----
    def snapshot(self) -> dict:
        self._ensure()
        self.page.wait_for_load_state("load")
        self.last = self.page.evaluate(SNAPSHOT_JS)
        self.last["status"] = self.last_status
        return self.last

    def render(self, snap: dict | None = None) -> str:
        s = snap or self.last
        lines = [f"URL: {s['url']} (HTTP {s.get('status')})", f"Title: {s.get('title')}", "Page text:", s.get("text", ""),
                 "Interactive elements:"]
        for e in s["elements"]:
            extra = ""
            if "options" in e:
                extra += f" options={e['options']}"
            if "value" in e:
                extra += f' value="{e["value"]}"'
            if "type" in e:
                extra += f" type={e['type']}"
            lines.append(f"[{e['ref']}] {e['role']} \"{e['name']}\"{extra}")
        return "\n".join(lines)

    def element(self, ref: str) -> dict | None:
        return next((e for e in self.last.get("elements", []) if e["ref"] == ref), None)

    def find(self, role: str, name: str) -> dict | None:
        """Used by procedure replay: locate an element by role + accessible name on a fresh snapshot."""
        self.snapshot()
        exact = [e for e in self.last["elements"] if e["role"] == role and e["name"].lower() == name.lower()]
        return exact[0] if exact else None

    def read_field(self, label: str) -> str | None:
        """Value shown next to a label: a table row (th/td) or a form field's current value."""
        self._ensure()
        return self.page.evaluate(r"""(label) => {
          const norm = t => (t || '').trim().replace(/\s+/g, ' ').toLowerCase();
          const want = norm(label);
          for (const th of document.querySelectorAll('th')) {
            if (norm(th.innerText) === want && th.nextElementSibling) return th.nextElementSibling.innerText.trim();
          }
          for (const l of document.querySelectorAll('label')) {
            if (norm(l.innerText) === want) {
              const el = document.getElementById(l.htmlFor);
              if (el) return el.tagName === 'SELECT' ? el.selectedOptions[0]?.text : el.value;
            }
          }
          return null;
        }""", label)

    def screenshot(self, path) -> str:
        self._ensure()
        self.page.screenshot(path=str(path), full_page=True)
        return str(path)

    # ---- actions ----
    def navigate(self, url: str) -> dict:
        self._ensure()
        full = urljoin(config.ERP_URL + "/", url)
        allowed = urlparse(config.ERP_URL).netloc
        if urlparse(full).netloc != allowed:
            raise PermissionError(f"Navigation outside the allowed company apps is blocked: {full}")
        self.page.goto(full)
        return self.snapshot()

    def _loc(self, ref: str):
        if not self.element(ref):
            raise LookupError(f"ref {ref} not in the current snapshot; take a new snapshot")
        return self.page.locator(f'[data-ref="{ref}"]')

    def click(self, ref: str) -> dict:
        self._ensure()
        self._loc(ref).click()
        self.page.wait_for_load_state("load")
        return self.snapshot()

    def fill(self, ref: str, value: str) -> dict:
        self._loc(ref).fill(str(value))
        return self.snapshot()

    def select(self, ref: str, option: str) -> dict:
        self._loc(ref).select_option(label=str(option))
        return self.snapshot()


BROWSER = Browser()
