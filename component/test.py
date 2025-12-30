import os,sys

current_file = os.path.abspath(__file__)
current_dir = os.path.dirname(current_file)
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

from component.check.DataCheck import *

if __name__ == '__main__':
    param = CheckParam(field_name=["132", "456"])
    check = NullValueCheck()
    sql = check.build_sql(param)
    print(param.where)