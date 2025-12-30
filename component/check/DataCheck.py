from abc import abstractmethod, ABC
from qgis._core import QgsFeatureRequest
from component.Logger import *
from .CheckParam import *

logger = setup_logger(__name__)


class DataCheck(ABC):
    # def __init__(self, layer):
    #     self.check_layer = layer

    class ColumnInfo:
        field_name = None
        type_name = None

    def _check(self, check_dto: CheckParam) -> []:
        requests = QgsFeatureRequest().setFilterExpression(check_dto.where)
        return check_dto.layer.getFeatures(requests)

    def _verify_sql(self, dto: CheckParam) -> bool:
        try:
            requests = QgsFeatureRequest().setFilterExpression(dto.where)
            dto.layer.getFeatures(requests)
            return True
        except Exception as e:
            logger.error(f"sql错误{dto.where}！")
            logger.error(e)
            return False

    @abstractmethod
    def _build_sql(self, dto: CheckParam) -> string:
        pass

    def check(self, dto: CheckParam) -> []:
        try:
            self._build_sql(dto)
            if self._verify_sql(dto):
                return self._check(dto)
            else:
                raise ValueError(f"sql语句执行失败！请检查")
        except Exception as e:
            logger.error(e)
            raise ValueError(f"sql语句执行失败！请检查")


class NullValueCheck(DataCheck):

    def _build_sql(self, dto: NullValueParam) -> string:
        fs = dto.field_name
        var = [f"({item} is null or {item} = '' or {item} = ' ')" for item in fs]
        where = " or ".join(var)
        dto.where = where


class DataValueCheck(DataCheck):

    def _build_sql(self, dto: CheckParam) -> string:
        values = dto.where.split(",")
        dto.where = "and".join([f"{item} not in ({','.join(values)})" for item in dto.field_name])
        logger.info(dto.where)


class LogicCheck(DataCheck):
    def _build_sql(self, dto: CheckParam) -> string:
        pass


"""
check_types = [
    "值域范围",
    "非空（必填）校验",
    "逻辑校验",
    "属性质检",
]
"""
check_service = {
    0: DataValueCheck(),
    1: NullValueCheck(),
    2: LogicCheck()
}
