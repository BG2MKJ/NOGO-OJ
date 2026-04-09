import sys
import json
import time
import random

N = 9
DIR4 = [(-1, 0), (0, -1), (1, 0), (0, 1)]
DIR8 = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]

# 棋盘：0 空，1 对应 requests 的颜色，-1 对应 responses 的颜色
board = [[0] * N for _ in range(N)]

# --- Zobrist Hash：给置换表做键 ---
_rng = random.Random(20260409)
ZOBRIST = [[[ _rng.getrandbits(64) for _ in range(2)] for _ in range(N)] for _ in range(N)]
current_hash = 0

# --- 置换表 ---
# key: (hash, color, depth)
# value: (score, best_move)
TT = {}

WIN = 10**9
TIME_LIMIT = 0.85  # 单步思考时间，保守一点


def in_board(x, y):
    return 0 <= x < N and 0 <= y < N


def color_index(c):
    return 0 if c == 1 else 1


def place_stone(x, y, color):
    """真正落子：更新棋盘和 zobrist hash。"""
    global current_hash
    board[x][y] = color
    current_hash ^= ZOBRIST[x][y][color_index(color)]


def remove_stone(x, y, color):
    """真正悔棋：更新棋盘和 zobrist hash。"""
    global current_hash
    board[x][y] = 0
    current_hash ^= ZOBRIST[x][y][color_index(color)]


def dfs_has_air(x, y, vis):
    """判断一个连通块是否有气。"""
    vis[x][y] = True
    c = board[x][y]
    has_air = False

    for dx, dy in DIR4:
        nx, ny = x + dx, y + dy
        if not in_board(nx, ny):
            continue
        if board[nx][ny] == 0:
            has_air = True
        elif board[nx][ny] == c and not vis[nx][ny]:
            if dfs_has_air(nx, ny, vis):
                has_air = True

    return has_air


def judge_available(x, y, color):
    """
    判断 (x, y) 对 color 是否是合法落子。
    不围棋规则：不能自杀，也不能提掉对方，因此所有块都必须仍有气。
    """
    if board[x][y] != 0:
        return False

    # 临时落子
    board[x][y] = color

    # 先检查自己这块有没有气
    vis = [[False] * N for _ in range(N)]
    if not dfs_has_air(x, y, vis):
        board[x][y] = 0
        return False

    # 再检查相邻棋块会不会因为这步而无气
    for dx, dy in DIR4:
        nx, ny = x + dx, y + dy
        if not in_board(nx, ny):
            continue
        if board[nx][ny] != 0 and not vis[nx][ny]:
            if not dfs_has_air(nx, ny, vis):
                board[x][y] = 0
                return False

    board[x][y] = 0
    return True


def local_priority(x, y):
    """
    候选点排序分数：
    1) 优先靠近已有棋子；
    2) 开局偏向中心。
    这是为了给 alpha-beta 更好的走法顺序。
    """
    if all(board[i][j] == 0 for i in range(N) for j in range(N)):
        return 100 - abs(x - 4) - abs(y - 4)

    around = 0
    for dx, dy in DIR8:
        nx, ny = x + dx, y + dy
        if in_board(nx, ny) and board[nx][ny] != 0:
            around += 1

    center_bias = 8 - abs(x - 4) - abs(y - 4)
    return around * 10 + center_bias


def get_legal_moves(color):
    """
    枚举 color 的所有合法点。
    做一个很常见的搜索剪枝：优先只看已有棋子附近的空位；
    如果全被剪没，再完整扫一遍，防止漏掉远处合法点。
    """
    moves = []
    has_stone = any(board[i][j] != 0 for i in range(N) for j in range(N))

    for i in range(N):
        for j in range(N):
            if board[i][j] != 0:
                continue

            if has_stone:
                near = False
                for dx, dy in DIR8:
                    ni, nj = i + dx, j + dy
                    if in_board(ni, nj) and board[ni][nj] != 0:
                        near = True
                        break
                if not near:
                    continue

            if judge_available(i, j, color):
                moves.append((i, j))

    # 邻近剪枝兜底
    if not moves:
        for i in range(N):
            for j in range(N):
                if board[i][j] == 0 and judge_available(i, j, color):
                    moves.append((i, j))

    moves.sort(key=lambda p: local_priority(p[0], p[1]), reverse=True)
    return moves


def evaluate(color):
    """
    叶子评估函数。
    不围棋里很实用的一种直觉：
    - 我能下的合法步越多越好；
    - 对手能下的合法步越少越好。
    """
    my_moves = get_legal_moves(color)
    opp_moves = get_legal_moves(-color)

    score = 18 * (len(my_moves) - len(opp_moves))

    # 再加一点候选点质量分，避免纯数量太粗糙
    if my_moves:
        score += sum(local_priority(x, y) for x, y in my_moves[:6])
    if opp_moves:
        score -= sum(local_priority(x, y) for x, y in opp_moves[:6])

    return score


def negamax(depth, alpha, beta, color, deadline):
    """
    纯算法搜索主干：
    negamax + alpha-beta + transposition table
    """
    # 超时就静态评估，防止 Botzone 判超时
    if time.perf_counter() >= deadline:
        return evaluate(color), None

    key = (current_hash, color, depth)
    if key in TT:
        return TT[key]

    legal = get_legal_moves(color)

    # 无合法步：当前执子方输
    if not legal:
        return -WIN - depth, None

    # 深度到头：静态评估
    if depth == 0:
        val = evaluate(color)
        TT[key] = (val, legal[0])
        return val, legal[0]

    best_score = -10**18
    best_move = legal[0]

    for x, y in legal:
        place_stone(x, y, color)
        child_score, _ = negamax(depth - 1, -beta, -alpha, -color, deadline)
        score = -child_score
        remove_stone(x, y, color)

        if score > best_score:
            best_score = score
            best_move = (x, y)

        if score > alpha:
            alpha = score
        if alpha >= beta:
            break

        if time.perf_counter() >= deadline:
            break

    TT[key] = (best_score, best_move)
    return best_score, best_move


def choose_move(my_color):
    """
    迭代加深：
    任意时刻超时，都至少能返回上一层深度的最好着法。
    """
    legal = get_legal_moves(my_color)
    if not legal:
        return (-1, -1)

    best_move = legal[0]
    deadline = time.perf_counter() + TIME_LIMIT

    depth = 1
    while True:
        if time.perf_counter() >= deadline:
            break

        score, move = negamax(depth, -10**18, 10**18, my_color, deadline)
        if move is not None:
            best_move = move

        # 搜到强制胜负，就没必要继续加深
        if abs(score) >= WIN // 2:
            break

        depth += 1

    return best_move


def rebuild_board(data):
    """
    按 Botzone 样例 Bot 的 requests / responses 接口恢复棋盘。
    这里保持和你现有样例的一致约定：
    - requests 记成 1
    - responses 记成 -1
    """
    turn_id = len(data["responses"])

    for i in range(turn_id):
        x = data["requests"][i]["x"]
        y = data["requests"][i]["y"]
        if x != -1:
            place_stone(x, y, 1)

        x = data["responses"][i]["x"]
        y = data["responses"][i]["y"]
        if x != -1:
            place_stone(x, y, -1)

    x = data["requests"][turn_id]["x"]
    y = data["requests"][turn_id]["y"]
    if x != -1:
        place_stone(x, y, 1)

    # 和你原样例保持一致：
    # request 为 (-1,-1) 表示我是先手，本方颜色视作 1
    # 否则我是后手，本方颜色视作 -1
    my_color = 1 if x == -1 else -1
    return my_color


def main():
    s = sys.stdin.readline().strip()
    if not s:
        return

    data = json.loads(s)
    my_color = rebuild_board(data)

    x, y = choose_move(my_color)
    print(json.dumps({"response": {"x": x, "y": y}}, separators=(",", ":")))


if __name__ == "__main__":
    main()