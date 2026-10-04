@powershell -c "gc ((gci runs -dir | sort LastWriteTime | select -last 1).FullName + '\report.md')"
