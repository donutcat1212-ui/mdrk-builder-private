# Локальное Windows-приложение с Microsoft Word

MVP реализуется на Python с Tkinter, `python-docx` и узким адаптером Microsoft Word COM. Word является обязательной зависимостью рабочего компьютера и преобразует DOC/RTF в временные DOCX; вся обработка выполняется локально, а PyInstaller-сборка проверяется на Windows, потому что PyInstaller не создаёт Windows-бинарии с macOS.
