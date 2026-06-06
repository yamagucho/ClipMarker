@echo off
cd /d "%~dp0"
set TAG=v0.1.0-beta

if not exist ".git" (
    git init -b main
    git remote add origin https://github.com/yamagucho/ClipMarker.git
)

git add .
git commit -m "Open beta release"
git push -u origin main
git tag v0.1.0-beta
git push origin v0.1.0-beta

echo.
echo Done! Check GitHub Actions for the build:
echo https://github.com/yamagucho/ClipMarker/actions
echo.
echo Release page (exe download):
echo https://github.com/yamagucho/ClipMarker/releases
echo.
pause
