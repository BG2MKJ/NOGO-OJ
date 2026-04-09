import sys
import json
import random

# 棋盘：0 表示空，1 表示对手/黑方请求中的棋，-1 表示我方 responses 中的棋
board = [[0 for _ in range(9)] for _ in range(9)]

# 四联通方向
DIRS = [(-1, 0), (0, -1), (1, 0), (0, 1)]


def in_border(x, y):
    return 0 <= x < 9 and 0 <= y < 9


# 判断一个连通块是否有气
def dfs_air(fx, fy, vis):
    vis[fx][fy] = True
    has_air = False

    for dx, dy in DIRS:
        nx, ny = fx + dx, fy + dy
        if in_border(nx, ny):
            if board[nx][ny] == 0:
                has_air = True
            elif board[nx][ny] == board[fx][fy] and not vis[nx][ny]:
                if dfs_air(nx, ny, vis):
                    has_air = True

    return has_air


# 判断 (fx, fy) 对 col 来说是否是合法落子
def judge_available(fx, fy, col):
    if board[fx][fy] != 0:
        return False

    board[fx][fy] = col
    vis = [[False for _ in range(9)] for _ in range(9)]

    # 先检查自己这块是否有气
    if not dfs_air(fx, fy, vis):
        board[fx][fy] = 0
        return False

    # 再检查相邻已有棋块，若因为这步导致某块无气，则此步非法
    for dx, dy in DIRS:
        nx, ny = fx + dx, fy + dy
        if in_border(nx, ny):
            if board[nx][ny] != 0 and not vis[nx][ny]:
                if not dfs_air(nx, ny, vis):
                    board[fx][fy] = 0
                    return False

    board[fx][fy] = 0
    return True


def main():
    s = sys.stdin.readline().strip()
    if not s:
        return

    data = json.loads(s)

    # 恢复历史局面
    turn_id = len(data["responses"])

    for i in range(turn_id):
        x = data["requests"][i]["x"]
        y = data["requests"][i]["y"]
        if x != -1:
            board[x][y] = 1

        x = data["responses"][i]["x"]
        y = data["responses"][i]["y"]
        if x != -1:
            board[x][y] = -1

    # 读入这一回合对手刚下的位置
    x = data["requests"][turn_id]["x"]
    y = data["requests"][turn_id]["y"]
    if x != -1:
        board[x][y] = 1

    # 如果这一步 request 是 (-1,-1)，说明我是先手黑方；否则我是后手白方
    my_color = 1 if x == -1 else -1

    # 枚举所有合法点
    available_list = []
    for i in range(9):
        for j in range(9):
            if judge_available(i, j, my_color):
                available_list.append((i, j))

    # 随机选一个合法点
    # 正常情况下不会没有合法点；若真没有，给个兜底输出
    if available_list:
        px, py = random.choice(available_list)
    else:
        px, py = -1, -1

    ret = {
        "response": {
            "x": px,
            "y": py
        }
    }

    print(json.dumps(ret, separators=(",", ":")))


if __name__ == "__main__":
    main()