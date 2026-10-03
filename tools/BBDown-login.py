# -*- coding: utf-8 -*-
"""B站扫码登录修复版 - 供 BBDown 1.6.3 使用.

B站改版后, 扫码登录成功的 cookie 通过 Set-Cookie 响应头返回,
不再写在 data.url 里. BBDown 1.6.3 只解析 data.url, 所以出现
"登录成功: SESSDATA=" 为空的假成功现象.

本脚本直接调用 B站登录接口, 生成二维码 -> 等待手机扫码 ->
从 Set-Cookie 提取登录 cookie -> 写入 BBDown.data, 登录后即可
直接使用 BBDown.exe 下载.
"""

import os
import shutil
import subprocess
import sys
import time
import urllib.parse

try:
    import qrcode
except ImportError:                  # 缺库时给一句人话, 别扔一串栈
    print("缺少 qrcode 模块, 无法生成二维码。请先安装: "
          "python -m pip install qrcode")
    sys.exit(1)

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJ = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(PROJ, "BBDown.data")
BACKUP_FILE = DATA_FILE + ".bak"
QR_FILE = os.path.join(PROJ, "qrcode.png")
KEY_FILE = os.path.join(PROJ, "qrcode_key.txt")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}
GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

AUTH_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def read_existing_data():
    """读取现有 BBDown.data 为字典, 保留原有字段."""
    cookies = {}
    if not os.path.exists(DATA_FILE):
        return cookies
    try:
        with open(DATA_FILE, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read().strip()
        for part in content.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip()
    except Exception as e:
        log("读取现有 BBDown.data 失败: %s" % e)
    return cookies


def _tighten_acl(path):
    """尽力把凭据文件的权限收紧到只有当前用户(失败就静默跳过).

    BBDown.data 里是长期有效的登录凭据, 默认继承的权限往往多给了
    Users/Everyone 读的份。icacls 普通用户就能改自己文件; 改不动(比如文件
    被占用/在特殊目录)也不影响登录本身, 所以失败不抛。
    """
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(["icacls", path, "/inheritance:r",
                        "/grant:r", "%s:(F)" % user],
                       capture_output=True, timeout=20)
    except Exception:
        pass


def write_data(cookies):
    """把登录信息写回 BBDown.data (BBDown 读取的登录文件).

    先写临时文件再改名: 写盘途中被杀/断电不会留下半截凭据文件, 也不会
    把旧的 BBDown.data 弄坏(那是要重新扫码才能恢复的)。
    """
    if os.path.exists(DATA_FILE):
        try:
            shutil.copy2(DATA_FILE, BACKUP_FILE)
            log("已备份原登录信息到 BBDown.data.bak")
        except Exception as e:
            log("备份失败(忽略): %s" % e)
    line = "; ".join("%s=%s" % (k, v) for k, v in cookies.items())
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(line)
        _tighten_acl(tmp)          # 先收紧权限再改名: 半路失败也不会留下宽松的凭据
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)      # 这份 .tmp 里是完整凭据, 绝不能留在目录里
        except OSError:
            pass
        log("写入 BBDown.data 失败: %s" % e)
        log("(临时文件已清理, 原来的登录信息没有被动过)")
        return False
    for path in (DATA_FILE, BACKUP_FILE):
        if os.path.exists(path):
            _tighten_acl(path)
    log("登录信息已写入 BBDown.data")
    return True


def generate_qr():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        r = session.get(GENERATE_URL, timeout=15)
        d = r.json()
    except Exception as e:
        log("获取二维码失败: %s" % e)
        sys.exit(1)
    if d.get("code") != 0:
        log("获取二维码失败: %s" % d)
        sys.exit(1)

    qrcode_key = d["data"]["qrcode_key"]
    qr_url = d["data"]["url"]

    try:
        img = qrcode.make(qr_url)
        img.save(QR_FILE)
        _tighten_acl(QR_FILE)      # 这张图能直接扫出登录态, 不给别人看
        try:
            os.startfile(QR_FILE)
        except Exception:
            pass
    except Exception as e:
        log("生成二维码图片失败: %s" % e)
        sys.exit(1)

    log("二维码已生成: %s" % QR_FILE)
    log("请打开上面的二维码图片, 用手机B站APP扫描并确认登录...")
    try:
        with open(KEY_FILE, "w", encoding="utf-8") as f:
            f.write(qrcode_key)
        _tighten_acl(KEY_FILE)
    except Exception as e:
        log("保存二维码密钥失败: %s" % e)
    return qrcode_key


def cleanup_qr_files():
    """登录成功后清掉二维码图片和密钥(留着只有害处, 没有用处)."""
    for path in (QR_FILE, KEY_FILE):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def poll_login(qrcode_key, timeout):
    session = requests.Session()
    session.headers.update(HEADERS)

    last_code = None
    success_data = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = session.get(
                POLL_URL,
                params={"qrcode_key": qrcode_key, "source": "main-fe-header"},
                timeout=15,
            )
            d = r.json()
        except Exception as e:
            log("轮询异常: %s" % e)
            time.sleep(2)
            continue

        data = d.get("data") or {}
        code = data.get("code", -1)
        if code != last_code:
            last_code = code
            if code == 86101:
                log("状态: 等待扫码...")
            elif code == 86090:
                log("状态: 扫码成功, 请在手机上确认...")
            elif code == 0:
                log("状态: 登录成功!")
            elif code == 86038:
                log("状态: 二维码已过期, 请重新运行")
                sys.exit(2)
            else:
                log("状态: code=%s %s" % (code, data.get("message", "")))

        if code == 0:
            success_data = data
            break
        time.sleep(2)

    if success_data is None:
        log("登录超时或未扫码, 未获取到登录信息")
        return None

    # 优先从 Set-Cookie 提取 (新版接口的返回位置)
    jar_cookies = dict(session.cookies)
    # 兜底: 部分情况 data.url 里仍可能带 SESSDATA
    url_cookies = {}
    if success_data.get("url"):
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(success_data["url"]).query
        )
        for k in AUTH_KEYS:
            if query.get(k):
                url_cookies[k] = query[k][0]

    if not (url_cookies.get("SESSDATA") or jar_cookies.get("SESSDATA")):
        log("登录失败: 未获取到 SESSDATA")
        return None

    merged = read_existing_data()
    for k in AUTH_KEYS:
        if url_cookies.get(k):
            # data.url 里的参数经过 parse_qs 解码, 需要按 BBDown 历史格式重新编码
            merged[k] = urllib.parse.quote(url_cookies[k], safe="-_")
        elif jar_cookies.get(k):
            merged[k] = jar_cookies[k]

    if not write_data(merged):
        return None
    cleanup_qr_files()
    log(
        "登录成功! DedeUserID=%s, SESSDATA长度=%d"
        % (merged.get("DedeUserID", "?"), len(merged.get("SESSDATA", "")))
    )
    log("现在可以直接运行 BBDown.exe 下载了")
    return merged


def main():
    import argparse

    ap = argparse.ArgumentParser(description="B站扫码登录(修复版)")
    ap.add_argument("--generate-only", action="store_true", help="只生成二维码后退出")
    ap.add_argument("--poll-key", default=None, help="使用已生成的二维码密钥继续轮询")
    ap.add_argument("--timeout", type=int, default=180, help="轮询超时秒数")
    args = ap.parse_args()

    log("=== B站扫码登录(修复版) ===")

    if args.poll_key:
        if poll_login(args.poll_key, args.timeout) is None:
            sys.exit(1)
        return

    qrcode_key = generate_qr()
    if args.generate_only:
        log("已只生成二维码, 密钥: %s" % qrcode_key)
        return

    if poll_login(qrcode_key, args.timeout) is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
