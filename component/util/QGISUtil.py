from qgis._core import QgsApplication, Qgis

mes_type_dic = {
    1:  Qgis.Success,
    2: Qgis.Warning,
    3: Qgis.Critical,
}

def sendMessageToTopParent(mes_type: int, mes_str: str,duration: int=3 ) -> None:
    """
    Args:
        duration:  持续展示时间
        mes_type: 1-> success, 2-> warning, 3-> error 消息等级
        mes_str: 消息内容

    Returns:

    """
    bar = QgsApplication.instance().property("messageBar")
    bar.pushMessage( mes_str, level=mes_type_dic[mes_type], duration=duration)