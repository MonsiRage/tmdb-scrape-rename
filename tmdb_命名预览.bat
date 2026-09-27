@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title TMDB rename preview

echo.
echo ========================================
echo   TMDB rename PREVIEW
echo   root: %CD%
echo   mode: preview only (no rename / no write)
echo   format: Title (Year) [tmdbid=ID]