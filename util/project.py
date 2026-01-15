from typing import Optional


class Point:
    def __init__(self, x: float, y: float, data):
        self.x = x
        self.y = y
        self.data = data

    def __repr__(self):
        return f"Point({self.x}, {self.y}, {self.data})"

class Rectangle:
    def __init__(self, x1, y1, x2, y2, data):
        """
        Args:
            x1: xmin
            y1: ymin
            x2: xmax
            y2: ymax
            data:
        """
        self.xmin = x1
        self.ymin = y1
        self.xmax = x2
        self.ymax = y2
        self.data = data
        self.width = x2 - x1
        self.height = y2 - y1

    def intersect(self, rect: "Rectangle"):
        """
        A 在 B 的左侧：x_2 \le x_3
        A 在 B 的右侧：x_1 \ge x_4
        A 在 B 的下方：y_2 \le y_3
        A 在 B 的上方：y_1 \ge y_4
        取反就是相交
        Args:
            rect:

        Returns: bool

        """
        return (not self.xmax < rect.xmin or self.xmin > rect.xmax
                or self.ymax < rect.ymin or self.ymin > rect.ymax)

    def contains(self, rect: "Rectangle"):
        """

        Args:
            rect:

        Returns:

        """
        return (rect.xmin>=self.xmin and rect.xmax<= self.xmax and
                rect.ymin>=self.ymin and rect.ymax<=self.ymax)

class Quadtree:
    def __init__(self):
        self.boundary = []
        self.capacity = 4
        self.boundary = None

        self.is_divided = False
        self.nw: Optional[Quadtree] = None #西北
        self.ne: Optional[Quadtree] = None # 东北
        self.sw: Optional[Quadtree] = None # 西南
        self.se: Optional[Quadtree] = None # 东南
        self.datas=[]

    def insert(self, point: Point, fid: int):
        self.datas.append({"id":fid, "point":point})

    def init_boundary(self):
        self.datas.sort(key=lambda e: e["point"].x)
        x_t = [self.datas[0].x, self.datas[-1]["point"].x]
        self.datas.sort(key=lambda e: e["point"].y)
        y_t = [self.datas[0].y, self.datas[-1]["point"].y]
        self.boundary = [x_t[0], y_t[0], x_t[-1], y_t[-1]]

    def build_tree(self):
        self.init_boundary()

    def skip(self, items):
        # 每次往下判断然后清除上一步的数据
        if len(items) <= self.capacity:
            return

        # 划分网格
        x_c = (self.boundary[0]+self.boundary[2])/2
        y_c = (self.boundary[1]+self.boundary[3])/2
        nw = Quadtree()

