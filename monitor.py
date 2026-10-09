
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from web3 import Web3

# 必须在托管平台的环境变量中设置
HTTP_RPC = os.environ["ETH_HTTP_RPC"]
SPENDER = Web3.to_checksum_address(os.environ["APPROVE_SPENDER"])
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# 使用当前目录，避免默认目录不存在或没有写入权限
DB_PATH = os.getenv("APPROVE_DB", "events.sqlite3")

POLL_SECONDS = 4
CONFIRMATIONS = 12
INITIAL_LOOKBACK = int(os.getenv("INITIAL_LOOKBACK", "0"))
CHUNK_SIZE = 100

APPROVAL_TOPIC = Web3.to_hex(
    Web3.keccak(text="Approval(address,address,uint256)")
)
SPENDER_TOPIC = "0x" + SPENDER.lower()[2:].zfill(64)


def now_text():
    return datetime.now(timezone.utc).isoformat()


def open_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            last_block INTEGER NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            block_number INTEGER NOT NULL,
            tx_hash TEXT NOT NULL,
            token TEXT NOT NULL,
            owner TEXT NOT NULL,
            spender TEXT NOT NULL,
            raw_amount TEXT NOT NULL,
            log_index INTEGER NOT NULL,
            notified INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.commit()
    return conn


def telegram_send(message):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TG_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }).encode("utf-8")

    request = Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError("Telegram API 返回失败")


def notify_pending(conn):
    rows = conn.execute("""
        SELECT event_id, block_number, tx_hash, token,
               owner, spender, raw_amount
        FROM events
        WHERE notified = 0
        ORDER BY block_number, log_index
        LIMIT 20
    """).fetchall()

    for row in rows:
        event_id, block, tx, token, owner, spender, amount = row

        message = (
            "🔔 ETH Approve 授权提醒\n\n"
            f"代币合约：{token}\n"
            f"授权人 owner：{owner}\n"
            f"被授权 spender：{spender}\n"
            f"授权额度（原始单位）：{amount}\n"
            f"区块高度：{block}\n"
            f"交易哈希：{tx}\n"
            f"Etherscan：https://etherscan.io/tx/{tx}"
        )

        try:
            telegram_send(message)
            conn.execute(
                "UPDATE events SET notified = 1 WHERE event_id = ?",
                (event_id,),
            )
            conn.commit()
            print("Telegram 推送成功：", tx, flush=True)

        except Exception as exc:
            print("Telegram 发送失败，将重试：", exc, flush=True)
            break


def decode_address(topic):
    value = Web3.to_hex(topic)
    return Web3.to_checksum_address("0x" + value[-40:])


def save_logs(conn, logs):
    for log in logs:
        topics = log["topics"]

        if len(topics) < 3:
            continue

        owner = decode_address(topics[1])
        spender = decode_address(topics[2])

        if spender.lower() != SPENDER.lower():
            continue

        tx_hash = Web3.to_hex(log["transactionHash"])
        block = int(log["blockNumber"])
        log_index = int(log["logIndex"])
        token = Web3.to_checksum_address(log["address"])
        raw_amount = str(int.from_bytes(log["data"], "big"))
        event_id = f"{tx_hash}:{log_index}"

        conn.execute("""
            INSERT OR IGNORE INTO events
            (event_id, block_number, tx_hash, token, owner,
             spender, raw_amount, log_index, notified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            event_id,
            block,
            tx_hash,
            token,
            owner,
            spender,
            raw_amount,
            log_index,
        ))

    conn.commit()


def scan_range(w3, conn, start, end):
    if start > end:
        return

    try:
        logs = w3.eth.get_logs({
            "fromBlock": start,
            "toBlock": end,
            "topics": [
                APPROVAL_TOPIC,
                None,
                SPENDER_TOPIC,
            ],
        })

        save_logs(conn, logs)

        conn.execute(
            "UPDATE state SET last_block = ? WHERE id = 1",
            (end,),
        )
        conn.commit()

        print(
            f"已扫描区块 {start}-{end}，"
            f"匹配日志 {len(logs)} 条",
            flush=True,
        )

    except Exception as exc:
        if start < end:
            mid = (start + end) // 2
            print(
                f"区块 {start}-{end} 查询失败，拆分重试：{exc}",
                flush=True,
            )
            scan_range(w3, conn, start, mid)
            scan_range(w3, conn, mid + 1, end)
        else:
            raise


def main():
    if not HTTP_RPC.startswith("https://"):
        raise ValueError("ETH_HTTP_RPC 必须是 HTTPS RPC 地址")

    w3 = Web3(Web3.HTTPProvider(
        HTTP_RPC,
        request_kwargs={"timeout": 30},
    ))

    conn = open_db()

    try:
        if not w3.is_connected():
            raise ConnectionError("无法连接 ETH HTTP RPC")

        if w3.eth.chain_id != 1:
            raise RuntimeError("RPC 不是 Ethereum 主网")

        latest = w3.eth.block_number
        row = conn.execute(
            "SELECT last_block FROM state WHERE id = 1"
        ).fetchone()

        if row is None:
            start = max(
                0,
                latest - CONFIRMATIONS - INITIAL_LOOKBACK,
            )
            conn.execute(
                "INSERT INTO state (id, last_block) VALUES (1, ?)",
                (start - 1,),
            )
            conn.commit()
            print("首次启动，开始区块：", start, flush=True)
        else:
            print(
                "从已保存游标恢复，下一次扫描从：",
                row[0] + 1,
                flush=True,
            )

        print("ETH 主网连接成功", flush=True)
        print("目标 spender：", SPENDER, flush=True)
        print("数据库路径：", DB_PATH, flush=True)

        while True:
            try:
                latest = w3.eth.block_number
                safe_end = latest - CONFIRMATIONS

                row = conn.execute(
                    "SELECT last_block FROM state WHERE id = 1"
                ).fetchone()

                next_block = row[0] + 1

                while next_block <= safe_end:
                    end = min(
                        next_block + CHUNK_SIZE - 1,
                        safe_end,
                    )
                    scan_range(w3, conn, next_block, end)
                    next_block = end + 1

                notify_pending(conn)
                time.sleep(POLL_SECONDS)

            except KeyboardInterrupt:
                break

            except Exception as exc:
                print(
                    "扫描异常，稍后重试：",
                    repr(exc),
                    flush=True,
                )
                time.sleep(5)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
