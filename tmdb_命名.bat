@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title TMDB rename

echo.
echo ========================================
echo   TMDB rename
echo   root: %CD%
echo   mode: APPLY renames + posters
echo   format: Title (Year) [tmdbid=ID]