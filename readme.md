

### 打包前需要
```commandline
D:\zsh\app\qgis_3.40\bin\python-qgis-ltr.bat -m pip install pyinstaller==5.13.2
 
 D:\zsh\app\qgis_3.40\bin\python-qgis-ltr.bat -m pip install fastapi uvicorn
```
### 移除控制

* 主窗口不可再其它子组件中引入，会导致底层崩溃，可在init是作为parent传入 