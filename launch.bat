@echo off
REM Launch the Iris GUI. If Anaconda is installed in the default location,
REM the conda environment named below is activated first.
set "IRIS_CONDA_ENV=capture"
if exist "%USERPROFILE%\anaconda3\condabin\conda.bat" (
    call "%USERPROFILE%\anaconda3\condabin\conda.bat" activate %IRIS_CONDA_ENV%
)
python "%~dp0gui.py"
pause
