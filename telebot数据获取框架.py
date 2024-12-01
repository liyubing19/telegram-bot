import logging
import signal
import threading
import time
from collections import defaultdict
from collections import deque
from functools import wraps
from logging.handlers import RotatingFileHandler

import mysql.connector.pooling
import schedule
import telebot


# 定义 escape_markdown 函数
def escape_markdown(text):
    """
    Escape markdown special characters in the given text.
    """
    escape_chars = ['\\', '`', '*', '_', '{', '}', '[', ']', '(', ')', '#', '+', '-', '.', '!']
    return ''.join(['\\' + char if char in escape_chars else char for char in text])


# 配置日志系统
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    handlers=[
                        RotatingFileHandler('app.log', maxBytes=1000000, backupCount=3, encoding='utf-8'),
                        logging.StreamHandler()
                    ])
logger = logging.getLogger(__name__)
#全局变量
bot_username = "BotFather"
# 创建 Telegram Bot 实例
bot_token = ""  # 请替换为你的 bot token
bot = telebot.TeleBot(bot_token)

# 数据库连接池配置
db_config = {
    "host": "localhost",
    "user": "root",
    "password": "admin",
}

# 设置数据库连接池，每个池10个连接
db_pools = {
    "users": mysql.connector.pooling.MySQLConnectionPool(pool_name="users_pool", pool_size=5, **db_config,
                                                         database="users"),
}

# 请求频率限制配置
user_requests = {}
request_limit = 5  # 用户5秒内只能请求一次
global_requests = deque()  # 全局请求时间戳队列
global_request_limit = 5  # 全球每秒最多5个请求


# 请求频率限制函数
def rate_limiter(user_id, current_time):
    # 检查全球请求率
    while global_requests and global_requests[0] < current_time - 1:
        global_requests.popleft()
    if len(global_requests) >= global_request_limit:
        logger.warning("全球请求频率已达到限制")
        return False

    # 检查用户请求频率
    if user_id in user_requests and current_time - user_requests[user_id] < request_limit:
        logger.warning(f"用户 {user_id} 请求过于频繁")
        return False

    # 更新记录
    user_requests[user_id] = current_time
    global_requests.append(current_time)
    logger.info(f"接受用户 {user_id} 的请求")
    return True


# 在全局范围初始化 tables_cache
tables_cache = {}


# 数据库查询函数
def search_database(phone_number, chat_id, reply_id, query_type):
    relevant_pools = {
        'phone': ['待添加库'],
        'id_card': ['待添加库']
    }.get(query_type, [])

    results = []

    for pool_name in relevant_pools:
        db_pool = db_pools[pool_name]
        database_query(phone_number, db_pool, query_type, results)

    if results:
        response_message = "\n\n".join(results)
        bot.edit_message_text(response_message, chat_id, reply_id, parse_mode='Markdown')
        logger.info(f"查询到数据: {response_message}")
    else:
        bot.edit_message_text("未找到数据", chat_id, reply_id)
        modify_user_points(chat_id, 1, db_pools['users'])  # 未找到数据，返还1积分
        logger.info("未查询到任何数据")


# 数据库查询函数
def database_query(phone_number, db_pool, query_type, results):
    global tables_cache
    conn = None
    cursor = None
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        if db_pool.pool_name not in tables_cache:
            cursor.execute("SHOW TABLES")
            tables_cache[db_pool.pool_name] = [table[0] for table in cursor.fetchall()]
        tables = tables_cache[db_pool.pool_name]
        for table_name in tables:
            query_column = "phone" if query_type == "phone" else "cardno"
            cursor.execute(f"SELECT * FROM {table_name} WHERE {query_column} = %s", (phone_number,))
            result = cursor.fetchall()
            for row in result:
                phone = row[2] if len(row) > 2 else None
                name = row[0]
                cardno = row[1]
                response_message = f"Name: `{name}`\nCardno: `{cardno}`"
                if phone:
                    response_message = f"Phone: `{phone}`\n" + response_message
                results.append(response_message)
    except Exception as e:
        logger.error(f"数据库查询错误: {str(e)}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# 用户积分修改函数
def modify_user_points(user_id, points_change, db_pool):
    conn = db_pool.get_connection()  # 从正确的数据库连接池获取连接
    cursor = conn.cursor()
    try:
        # 查询当前用户的积分
        cursor.execute("SELECT Points FROM users WHERE UserID = %s", (str(user_id),))
        result = cursor.fetchone()
        if result:
            current_points = result[0] or 0  # 如果查询结果为None，则设为0
            new_points = max(0, current_points + points_change)  # 计算新积分，不允许为负数

            # 更新用户的积分
            cursor.execute("UPDATE users SET Points = %s WHERE UserID = %s", (new_points, str(user_id)))
            conn.commit()
            logger.info(f"用户 {user_id} 的积分已更新为 {new_points}")
        else:
            logger.warning(f"没有找到用户ID为 {user_id} 的用户，无法更新积分")
    except Exception as e:
        logger.error(f"更新用户 {user_id} 的积分时出错: {str(e)}")
        conn.rollback()  # 回滚事务
    finally:
        cursor.close()
        conn.close()


# 定义检查用户是否在指定频道中的装饰器
def check_channel_membership(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id
        channel_id = -12345678910
        #待修改频道id
        if is_user_in_channel(channel_id, user_id):
            return func(message, *args, **kwargs)
        else:
            bot.send_message(chat_id=message.chat.id,
                             text="使用机器人需要加入下方频道：https://t.me/频道链接")
            return

    return wrapper


def is_user_in_channel(channel_id, user_id):
    try:
        # 获取频道成员信息
        member = bot.get_chat_member(channel_id, user_id)
        # 检查成员是否在频道中
        if member and member.status != 'left':
            return True
    except Exception as e:
        print(f"Error checking channel membership: {e}")
    return False


# 创建字典记录每个用户的邀请时间戳
invite_timestamps = defaultdict(list)


# 处理/start命令
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    username = message.from_user.username
    user_id_str = str(user_id)

    if not username:
        username = "default_username"  # 默认用户名设置

    conn = db_pools['users'].get_connection()
    cursor = conn.cursor()
    try:
        # 检查用户是否已经注册过
        cursor.execute("SELECT UserID FROM users WHERE UserID = %s", (user_id_str,))
        result = cursor.fetchone()

        if result:
            bot.send_message(message.chat.id, f"你好，{user_name}，欢迎回来！")
            logger.info(f"用户 {user_id_str} 已经注册过，用户信息更新")
        else:
            # 处理邀请者信息
            invite_user_id = None
            try:
                invite_code = message.text.split(' ')[1]
                if len(invite_code) > 0:
                    # 提取邀请者的用户ID
                    invite_user_id = int(invite_code)

                    # 检查邀请者是否存在
                    cursor.execute("SELECT UserID FROM users WHERE UserID = %s", (str(invite_user_id),))
                    invite_user = cursor.fetchone()
                    if not invite_user:
                        bot.send_message(message.chat.id, "邀请者不存在，请检查邀请链接是否正确。")
                        logger.warning(f"邀请者 {invite_user_id} 不存在")
                        invite_user_id = None  # 重置邀请者ID
            except (IndexError, ValueError):
                pass

            # 插入或更新用户信息
            insert_or_update_user(user_id, user_name, username, db_pools['users'])

            if invite_user_id:
                # 检查一分钟内的邀请次数
                current_time = time.time()
                invite_timestamps[invite_user_id] = [timestamp for timestamp in invite_timestamps[invite_user_id] if
                                                     current_time - timestamp < 60]

                if len(invite_timestamps[invite_user_id]) >= 5:
                    # 修改邀请者的 account_status 为 1
                    cursor.execute("UPDATE users SET account_status = 1 WHERE UserID = %s", (str(invite_user_id),))
                    conn.commit()

                    bot.send_message(invite_user_id, "您的邀请存在异常，已上报管理。")
                    bot.send_message(message.chat.id, "你好，{user_name}，注册成功！")
                    logger.warning(f"邀请者 {invite_user_id} 邀请次数超过限制，账户状态已修改为 1")
                else:
                    invite_timestamps[invite_user_id].append(current_time)

                    # 如果有邀请者，记录邀请信息并增加积分
                    cursor.execute("UPDATE users SET InvitationsCount = InvitationsCount + 1 WHERE UserID = %s",
                                   (str(invite_user_id),))
                    conn.commit()
                    modify_user_points(invite_user_id, 3, db_pools['users'])  # 调用现有的函数增加积分

                    # 查询邀请者的总邀请数
                    cursor.execute("SELECT InvitationsCount FROM users WHERE UserID = %s", (str(invite_user_id),))
                    total_invites = cursor.fetchone()[0]

                    bot.send_message(message.chat.id, f"你好，{user_name}，注册成功！感谢 {invite_user_id} 的邀请。")
                    bot.send_message(invite_user_id,
                                     f"你邀请了 {user_id_str} 获得了 3 积分\n你已经邀请了 {total_invites} 人")
                    logger.info(f"用户 {user_id_str} 注册成功，由 {invite_user_id} 邀请")
            else:
                bot.send_message(message.chat.id, f"你好，{user_name}")
                logger.info(f"用户 {user_id_str} 注册成功，没有邀请者")
    except Exception as e:
        logger.error(f"检查用户是否注册时出错: {str(e)}")
        bot.send_message(message.chat.id, "注册过程中出现错误，请稍后再试。")
    finally:
        cursor.close()
        conn.close()


def insert_or_update_user(user_id, name, username, db_pool):
    conn = db_pool.get_connection()
    cursor = conn.cursor()
    user_id_str = str(user_id)  # 在 try 块外面定义和初始化 user_id_str
    try:
        cursor.execute("""
            INSERT INTO users (UserID, Name, Username, Points, CheckedIn, InvitationsCount, account_status) 
            VALUES (%s, %s, %s, 0, 0, 0, 0)
            ON DUPLICATE KEY UPDATE Name = VALUES(Name), Username = VALUES(Username);
            """, (user_id_str, name, username))
        conn.commit()
        logger.info(f"插入或更新用户：{user_id_str}, {name}, {username}")
    except Exception as e:
        logger.error(f"插入/更新用户 {user_id_str} 时出错: {str(e)}")
    finally:
        cursor.close()
        conn.close()


@bot.message_handler(commands=['share'])
def handle_share_command(message):
    try:
        user_id = message.from_user.id
        bot_username = bot.get_me().username
        invite_link = f"https://t.me/{bot_username}?start={user_id}"
        response_message = (
            f"这是你的专属邀请链接：\n{invite_link}\n"
            "分享给你的朋友来邀请他们吧！"
        )

        bot.send_message(message.chat.id, response_message)
        logger.info(f"用户 {user_id} 获取了邀请链接: {invite_link}")

    except Exception as e:
        logger.error(f"处理邀请链接指令时出错: {e}")
        bot.send_message(message.chat.id, "生成邀请链接时发生错误，请稍后再试。")


# 处理用户签到指令
@bot.message_handler(commands=['sign'])
@check_channel_membership
def handle_sign_in(message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    conn = db_pools['users'].get_connection()
    cursor = conn.cursor()
    try:
        # 检查用户是否已经签到
        cursor.execute("SELECT CheckedIn FROM users WHERE UserID = %s", (str(user_id),))
        result = cursor.fetchone()
        if result and result[0] == 1:
            bot.send_message(chat_id, "你今天已经签到过了。")
            logger.info(f"用户 {user_id} 尝试重复签到")
            return

        # 用户积分加2
        modify_user_points(user_id, 2, db_pools['users'])

        # 更新CheckedIn状态为1
        cursor.execute("UPDATE users SET CheckedIn = 1 WHERE UserID = %s", (str(user_id),))
        conn.commit()
        bot.send_message(chat_id, "签到成功！")
        logger.info(f"用户 {user_id} 签到成功并增加了积分")
    except Exception as e:
        logger.error(f"处理用户 {user_id} 签到时出错: {str(e)}")
        bot.send_message(chat_id, "签到过程中出现错误，请稍后重试。")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()


# 处理获取个人信息指令
@bot.message_handler(commands=['info'])
@check_channel_membership
def send_user_information(message):
    user_id = message.from_user.id
    first_name = escape_markdown(message.from_user.first_name)  # 转义名字
    username = escape_markdown(message.from_user.username) if message.from_user.username else "未设置"  # 转义用户名
    user_id_str = str(user_id)  # 用户的ID

    conn = db_pools['users'].get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT Points, CheckedIn, InvitationsCount FROM users WHERE UserID = %s", (user_id_str,))
        result = cursor.fetchone()
        points, checked_in_status, total_invites = (result if result else (0, False, 0))
        checked_in_text = "已签到" if checked_in_status else "未签到"

        info_message = f"""👤 *个人信息*:\n
🧑🏻‍💻 *我的用户名:* @{username}
🆔 *我的ID:* `{user_id_str}`
🔍 *查询积分:* {points}
👑 *邀请人数:* {total_invites}
✍🏻 *签到状态:* {escape_markdown(checked_in_text)}"""  # 加粗关键字段
        bot.send_message(message.chat.id, info_message, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"获取或更新用户 {user_id} 的信息时出错: {str(e)}")
        bot.send_message(message.chat.id, "获取用户信息时发生错误，请稍后再试。")
    finally:
        cursor.close()
        conn.close()


# 处理用户发送的手机号和身份证号指令
@bot.message_handler(func=lambda message: message.text.isdigit() or (
        len(message.text) == 18 and message.text[:-1].isdigit() and message.text[-1].upper() == 'X'))
@check_channel_membership
def handle_message(message):
    logger.info(f"接收到来自用户 {message.from_user.id} 的消息: {message.text}")
    current_time = time.time()

    # 首先检查请求频率限制
    if not rate_limiter(message.from_user.id, current_time):
        bot.reply_to(message, "请求太频繁，请稍后再试。")
        return

    # 检查用户是否已初始化
    conn = db_pools['users'].get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT UserID, Points FROM users WHERE UserID = %s", (str(message.from_user.id),))
        result = cursor.fetchone()
        if not result or not result[0]:  # 如果没有找到记录或用户未初始化
            bot.reply_to(message, "请先发送 /start 来初始化。")
            return
        # 检查积分是否足够
        current_points = result[1]
        if current_points < 1:
            bot.reply_to(message, "积分不够啦!")
            return
    finally:
        cursor.close()
        conn.close()

    # 积分足够，扣除1积分并继续查询
    modify_user_points(message.from_user.id, -1, db_pools['users'])

    # 处理手机号或身份证号
    input_text = message.text
    if len(input_text) == 11 and input_text.isdigit():
        query_type = "phone"
    elif len(input_text) == 18 and (input_text[:-1].isdigit() and input_text[-1].upper() == 'X'):
        query_type = "id_card"
    elif len(input_text) == 18 and input_text.isdigit():
        query_type = "id_card"
    else:
        query_type = None

    if query_type is None:
        bot.reply_to(message, "请输入有效的11位手机号或18位身份证号。")
        return

    reply = bot.send_message(message.chat.id, "检索中...")
    search_database(input_text, message.chat.id, reply.message_id, query_type)


# 每日签到记录自动清零函数
def reset_checked_in_daily():
    conn = db_pools['users'].get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET CheckedIn = 0")
        conn.commit()
        logger.info("所有用户的CheckedIn列已重置为0")
    except Exception as e:
        logger.error(f"重置CheckedIn列时出错: {str(e)}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()


# 安排任务每天23:59执行
schedule.every().day.at("23:59").do(reset_checked_in_daily)


# 在一个无限循环中运行计划任务
def run_scheduled_tasks():
    while True:
        schedule.run_pending()
        time.sleep(1)


def signal_handler(signum, frame):
    print("\n收到信号：", signum)
    logger.info("收到终止信号，机器人正在关闭")
    bot.stop_polling()
    exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# 启动定时任务的线程，以便不干扰主bot轮询线程
task_thread = threading.Thread(target=run_scheduled_tasks)
task_thread.start()

while True:
    try:
        bot.polling(none_stop=True, interval=1, timeout=30)
    except Exception as ex:
        logger.error(f"机器人掉线或报错，尝试自动重启: {ex}")
