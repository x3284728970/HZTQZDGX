#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
火种VPN 一体化节点工具（最终版 · 两轮提取 + 失败诊断）

两种运行模式：
  1. 手动模式：填写 CONFIG 中的 username / password，直接提取节点
  2. 自动模式：两者留空，自动注册主号 → 刷小号 → 兑换VIP → 提取节点

核心特性：
  - Token 缓存：登录成功后保存，下次运行直接复用，跳过注册/兑换流程
  - 账号持久化：主号存本地，VIP 未过期直接复用
  - 两轮提取：首轮快速扫描（10线程/5s超时），失败节点补漏轮（4线程/10s超时）
  - 失败原因诊断：统计 timeout / http_xxx / unavailable / error 分布
  - 边提取边写文件：每成功一个节点立即落盘，超时也不丢结果
  - VLESS Reality 补全 flow，Trojan 强制 ALPN http/1.1
  - 输出路径自适应 Android / Windows / Linux

依赖：
  pip install requests ddddocr pillow urllib3
"""

import os
import sys

# 屏蔽 onnxruntime 日志（必须在 import ddddocr 之前）
os.environ["ONNXRUNTIME_LOG_SEVERITY_LEVEL"] = "3"

import json
import re
import time
import random
import string
import uuid
import base64
import urllib.parse
import threading
import concurrent.futures
from typing import List, Dict, Optional, Tuple

import requests
import urllib3

# ==================== Windows 控制台 UTF-8 ====================
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import ddddocr
    OCR_AVAILABLE = True
    _ocr_engine = ddddocr.DdddOcr(show_ad=False)
except ImportError:
    OCR_AVAILABLE = False
    _ocr_engine = None

# ==================== 配置区域 ====================
CONFIG = {
    "username": "",
    "password": "",

    "auth_servers": [
        "https://server2k.hzzf.cc/realms/vpn_application/protocol/openid-connect/token",
        "https://154.17.1.102/realms/vpn_application/protocol/openid-connect/token",
        "https://kc.huozhong.us/realms/vpn_application/protocol/openid-connect/token",
    ],
    "api_servers": [
        "https://8.218.46.170/api/nodesystem/user",
        "https://154.17.0.133/api/nodesystem/user",
        "https://api.huozhong.us/api/nodesystem/user",
    ],
    "user_servers": [
        "https://server1a.hzzf.cc",
    ],
    "app_servers": [
        "https://8.218.46.170",
    ],

    "client_id": "vpn-user",
    "client_secret": "i16bYq4sXxlGl3s",

    # ---- 自动化参数 ----
    "sub_account_count": 1,
    "max_workers": 10,             # 首轮提取并发数
    "timeout": 5,                  # 首轮单请求超时（秒）
    "retry_workers": 4,            # 补漏轮并发数
    "retry_timeout": 10,           # 补漏轮单请求超时（秒）
    "login_timeout": 12,           # 登录/注册等关键请求超时（秒）
    "register_delay_min": 0,
    "register_delay_max": 0,

    # ---- 输出 ----
    "output_file": "huozhong_nodes.txt",
    "region_sort_order": ["香港", "新加坡", "日本", "台湾", "韩国", "美国"],
}

ACCOUNT_FILE = os.path.join(os.path.expanduser("~"), ".huozhong_account.json")
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".huozhong_token.json")
PARTIAL_FILE_SUFFIX = ".partial.tmp"

def resolve_output_path() -> str:
    if CONFIG.get("output_file"):
        return CONFIG["output_file"]
    if os.path.exists("/storage/emulated/0/Download"):
        return "/storage/emulated/0/Download/huozhong_links.txt"
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    if os.path.exists(downloads):
        return os.path.join(downloads, "huozhong_links.txt")
    return os.path.join(os.path.expanduser("~"), "huozhong_links.txt")

OUTPUT_FILE = resolve_output_path()
PARTIAL_FILE = OUTPUT_FILE + PARTIAL_FILE_SUFFIX

# ==================== 国旗映射 ====================
REGION_FLAGS = {
    "香港": "🇭🇰", "澳门": "🇲🇴", "台湾": "🇹🇼", "新加坡": "🇸🇬", "马来西亚": "🇲🇾",
    "泰国": "🇹🇭", "越南": "🇻🇳", "日本": "🇯🇵", "韩国": "🇰🇷", "印度": "🇮🇳",
    "菲律宾": "🇵🇭", "印度尼西亚": "🇮🇩", "柬埔寨": "🇰🇭", "沙特阿拉伯": "🇸🇦",
    "阿联酋": "🇦🇪", "以色列": "🇮🇱", "土耳其": "🇹🇷", "俄罗斯": "🇷🇺", "德国": "🇩🇪",
    "英国": "🇬🇧", "法国": "🇫🇷", "荷兰": "🇳🇱", "西班牙": "🇪🇸", "意大利": "🇮🇹",
    "瑞士": "🇨🇭", "瑞典": "🇸🇪", "挪威": "🇳🇴", "丹麦": "🇩🇰", "芬兰": "🇫🇮",
    "波兰": "🇵🇱", "捷克": "🇨🇿", "奥地利": "🇦🇹", "爱尔兰": "🇮🇪", "比利时": "🇧🇪",
    "葡萄牙": "🇵🇹", "希腊": "🇬🇷", "匈牙利": "🇭🇺", "美国": "🇺🇸", "加拿大": "🇨🇦",
    "墨西哥": "🇲🇽", "巴西": "🇧🇷", "阿根廷": "🇦🇷", "智利": "🇨🇱", "哥伦比亚": "🇨🇴",
    "澳大利亚": "🇦🇺", "关岛": "🇬🇺", "新西兰": "🇳🇿", "卡塔尔": "🇶🇦", "巴基斯坦": "🇵🇰",
    "埃及": "🇪🇬", "南非": "🇿🇦", "乌克兰": "🇺🇦", "伊朗": "🇮🇷", "科威特": "🇰🇼",
    "立陶宛": "🇱🇹", "保加利亚": "🇧🇬", "塞尔维亚": "🇷🇸", "北马其顿": "🇲🇰",
    "罗马尼亚": "🇷🇴", "冰岛": "🇮🇸", "拉脱维亚": "🇱🇻", "哈萨克斯坦": "🇰🇿",
    "斯洛伐克": "🇸🇰", "摩尔多瓦": "🇲🇩", "阿塞拜疆": "🇦🇿", "阿尔巴尼亚": "🇦🇱",
    "爱沙尼亚": "🇪🇪", "尼日利亚": "🇳🇬", "缅甸": "🇲🇲", "南极洲": "🇦🇶",
    "乌兹别克斯坦": "🇺🇿", "乌拉圭": "🇺🇾", "亚美尼亚": "🇦🇲", "伊拉克": "🇮🇶",
    "克罗地亚": "🇭🇷", "卢森堡": "🇱🇺", "危地马拉": "🇬🇹", "厄瓜多尔": "🇪🇨",
    "哥斯达黎加": "🇨🇷", "塞浦路斯": "🇨🇾", "尼泊尔": "🇳🇵", "摩洛哥": "🇲🇦",
    "斯洛文尼亚": "🇸🇮", "格鲁吉亚": "🇬🇪", "玻利维亚": "🇧🇴", "秘鲁": "🇵🇪",
    "阿曼": "🇴🇲", "巴林": "🇧🇭", "列支敦士登": "🇱🇮", "孟加拉国": "🇧🇩",
    "白俄罗斯": "🇧🇾", "未知": "🌐",
}

COMMON_HEADERS = {
    "User-Agent": "ktor-client",
    "Accept": "application/json",
    "Accept-Charset": "UTF-8",
    "X-App-Version": "1.1.17",
    "X-Device-OS": "Android",
}

# ==================== 线程安全 Session ====================
_thread_local = threading.local()

def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.verify = False
        _thread_local.session = s
    return _thread_local.session

# ==================== 重试装饰器 ====================
def retry_request(max_retries=2, backoff_factor=1.5):
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries:
                        raise
                    wait = backoff_factor ** (attempt + 1) * (0.5 + 0.3 * random.random())
                    print(f"  [重试 {attempt+1}/{max_retries}] {e}，{wait:.1f}s 后重试...")
                    time.sleep(wait)
            return None
        return wrapper
    return decorator

# ==================== 工具函数 ====================
def random_string(length_range=(7, 9)) -> str:
    return ''.join(random.choice(string.ascii_letters + string.digits)
                   for _ in range(random.randint(*length_range)))

def get_country_from_name(name: str) -> str:
    for country in sorted(REGION_FLAGS.keys(), key=len, reverse=True):
        if country in name:
            return country
    shortcuts = {"港": "香港", "台": "台湾", "新": "新加坡",
                 "日": "日本", "韩": "韩国", "美": "美国"}
    for k, v in shortcuts.items():
        if k in name:
            return v
    return "未知"

def parse_node_name_for_sort(name: str) -> Tuple[str, int]:
    match = re.match(r'(.+?)[-_]?(\d+)$', name)
    if match:
        return match.group(1).strip(), int(match.group(2))
    return name, 0

# ==================== 缓存持久化 ====================
def load_json_file(path: str) -> Optional[Dict]:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None

def save_json_file(path: str, data: Dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def load_saved_account() -> Optional[Dict]:
    return load_json_file(ACCOUNT_FILE)

def save_account(acc: Dict):
    save_json_file(ACCOUNT_FILE, acc)

def load_saved_token() -> Optional[Dict]:
    return load_json_file(TOKEN_FILE)

def save_token(token: str, expires_in: int):
    data = {
        "token": token,
        "expires_at": time.time() + expires_in - 60,
        "saved_at": time.time(),
    }
    save_json_file(TOKEN_FILE, data)

def get_cached_token() -> Optional[str]:
    data = load_saved_token()
    if data and data.get("token") and data.get("expires_at", 0) > time.time():
        return data["token"]
    return None

def clear_token_cache():
    try:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
    except Exception:
        pass

# ==================== 认证 ====================
@retry_request(max_retries=2)
def login(username: str, password: str) -> str:
    payload = {
        "client_id": CONFIG["client_id"],
        "client_secret": CONFIG["client_secret"],
        "grant_type": "password",
        "username": username,
        "password": password,
    }
    headers = dict(COMMON_HEADERS)
    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

    for auth_url in CONFIG["auth_servers"]:
        try:
            resp = get_session().post(auth_url, data=payload, headers=headers,
                                      timeout=CONFIG.get("login_timeout", 12))
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("access_token")
                if token:
                    expires = data.get("expires_in", 3600)
                    print(f"  [OK] 登录成功，Token 有效约 {expires // 60} 分钟")
                    save_token(token, expires)
                    return token
        except Exception:
            continue
    raise Exception("所有认证服务器均不可用")

# ==================== 验证码与注册 ====================
def solve_captcha() -> Optional[str]:
    if not OCR_AVAILABLE or _ocr_engine is None:
        print("  [ERROR] ddddocr 不可用，自动模式需要: pip install ddddocr")
        return None

    for attempt in range(1, 16):
        try:
            resp = get_session().get(
                f"{CONFIG['user_servers'][0]}/captcha/generate",
                timeout=CONFIG.get("login_timeout", 12))
            data = resp.json()
            img_bytes = base64.b64decode(data["imageBase64"])

            img_for_ocr = img_bytes
            try:
                import io
                from PIL import Image
                img = Image.open(io.BytesIO(img_bytes)).convert("L")
                img = img.resize((img.width * 2, img.height * 2))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                img_for_ocr = buf.getvalue()
            except Exception:
                pass

            captcha_text = _ocr_engine.classification(img_for_ocr)
            print(f"  [验证码] 第 {attempt} 次识别: {captcha_text}")

            val_resp = get_session().post(
                f"{CONFIG['user_servers'][0]}/captcha/validate",
                json={"captchaId": data["captchaId"], "userInput": captcha_text},
                timeout=CONFIG.get("login_timeout", 12))
            val_data = val_resp.json()
            if val_data.get("valid"):
                return val_data["token"]
        except Exception as e:
            print(f"  [!] 验证码异常: {e}")
        time.sleep(0.5)
    return None

def register_account(is_main: bool = True, ref_code: str = "") -> Optional[Dict]:
    username = random_string((7, 9)).lower()
    password = random_string((12, 15))
    device_id = str(uuid.uuid4())
    role = "主号" if is_main else "小号"
    print(f"[*] 注册{role}: {username}")

    captcha_token = solve_captcha()
    if not captcha_token:
        return None

    payload = {
        "username": username,
        "password": password,
        "email": "",
        "promoCode": ref_code if not is_main else "",
        "deviceId": device_id,
    }
    resp = get_session().post(
        f"{CONFIG['user_servers'][0]}/users",
        params={"captchaToken": captcha_token},
        json=payload,
        timeout=CONFIG.get("login_timeout", 12))
    if resp.status_code in (200, 201):
        print(f"  [+] {role}注册成功")
        return {"username": username, "password": password, "deviceId": device_id}
    print(f"  [!] 注册失败: {resp.text[:200]}")
    return None

# ==================== 邀请与 VIP ====================
@retry_request(max_retries=2)
def get_referral_code(token: str) -> str:
    headers = dict(COMMON_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    for app in CONFIG["app_servers"]:
        try:
            resp = get_session().get(
                f"{app}/v1/referral/v1.2/referral-poster/en_poster_1",
                headers=headers, timeout=CONFIG.get("login_timeout", 12))
            print(f"  [DEBUG] get_referral_code {app} -> HTTP {resp.status_code}")
            if resp.status_code == 200:
                code = resp.json().get("referralCode")
                if code:
                    return code
                else:
                    print(f"  [DEBUG] 响应内容: {resp.text[:300]}")
            else:
                print(f"  [DEBUG] 响应内容: {resp.text[:300]}")
        except Exception as e:
            print(f"  [DEBUG] {app} 异常: {e}")
            continue
    raise Exception("获取邀请码失败")

def submit_referral(sub_token: str, device_id: str, ref_code: str) -> bool:
    headers = dict(COMMON_HEADERS)
    headers["Authorization"] = f"Bearer {sub_token}"
    try:
        get_session().post(
            f"{CONFIG['user_servers'][0]}/users/loginDeviceInfo",
            json={"deviceId": device_id, "deviceOS": "Android", "deviceType": "Mobile"},
            headers=headers, timeout=CONFIG.get("login_timeout", 12))
        resp = get_session().post(
            f"{CONFIG['user_servers'][0]}/v1/referral/submit-referral",
            params={"referralCode": ref_code},
            headers=headers, timeout=CONFIG.get("login_timeout", 12))
        data = resp.json()
        if data.get("status") == "SUCCESSFUL":
            return True
        print(f"  [!] 绑定失败: {data}")
    except Exception as e:
        print(f"  [!] 邀请绑定异常: {e}")
    return False

@retry_request(max_retries=2)
def redeem_vip(token: str) -> bool:
    headers = dict(COMMON_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    for app in CONFIG["app_servers"]:
        try:
            resp = get_session().post(
                f"{app}/v1/credits/redeem/2",
                headers=headers, timeout=CONFIG.get("login_timeout", 12))
            print(f"  [DEBUG] redeem_vip {app} -> HTTP {resp.status_code}")
            if resp.status_code == 200:
                return True
            else:
                print(f"  [DEBUG] 响应内容: {resp.text[:300]}")
        except Exception as e:
            print(f"  [DEBUG] redeem_vip {app} 异常: {e}")
            continue
    raise Exception("兑换 VIP 失败")

def check_vip_status(token: str) -> bool:
    headers = dict(COMMON_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    for app in CONFIG["app_servers"]:
        try:
            resp = get_session().get(f"{app}/users/role", headers=headers,
                                     timeout=CONFIG.get("login_timeout", 12))
            if resp.status_code != 200:
                continue
            text = resp.text.strip()
            try:
                data = json.loads(text)
                role = str(data.get("role") or data.get("status") or data.get("name") or "")
            except Exception:
                role = text
            if "vip" in role.lower():
                print(f"[+] 当前角色: {role}")
                return True
            print(f"[!] 当前角色: {role}，非 VIP")
            return False
        except Exception:
            continue
    return False

# ==================== 节点链接生成 ====================
def generate_vless_link(config: Dict, node_name: str) -> str:
    vnext = config["settings"]["vnext"][0]
    user = vnext["users"][0]
    stream = config.get("streamSettings", {})
    network = stream.get("network", "tcp")

    params = {
        "encryption": user.get("encryption", "none"),
        "type": network,
    }
    if flow := user.get("flow"):
        params["flow"] = flow

    if stream.get("security") == "reality":
        reality = stream.get("realitySettings", {})
        params.update({
            "security": "reality",
            "pbk": reality.get("publicKey", ""),
            "fp": reality.get("fingerprint", "chrome"),
            "sni": reality.get("serverName", ""),
            "sid": reality.get("shortId", ""),
            "headerType": "none",
        })
    elif stream.get("security") == "tls":
        tls = stream.get("tlsSettings", {})
        params["security"] = "tls"
        if sni := tls.get("serverName"):
            params["sni"] = sni
        if tls.get("allowInsecure") is not None:
            params["allowInsecure"] = "1" if tls.get("allowInsecure") else "0"
        if fp := tls.get("fingerprint"):
            params["fp"] = fp
        params["alpn"] = "http/1.1"

    if network == "ws":
        ws = stream.get("wsSettings", {})
        if path := ws.get("path"):
            params["path"] = path
        params["host"] = params.get("sni", "vpn-node.internal")
    elif network == "grpc":
        grpc = stream.get("grpcSettings", {})
        if svc := grpc.get("serviceName"):
            params["serviceName"] = svc

    params = {k: v for k, v in params.items() if v not in (None, "")}
    query = urllib.parse.urlencode(params)
    remark = urllib.parse.quote(node_name)
    return f"vless://{user['id']}@{vnext['address']}:{vnext['port']}?{query}#{remark}"

def generate_trojan_link(config: Dict, node_name: str) -> str:
    servers = config.get("settings", {}).get("servers", [])
    if not servers:
        raise ValueError("无 servers 配置")
    s = servers[0]
    address, port, password = s.get("address"), s.get("port"), s.get("password")
    if not all([address, port, password]):
        raise ValueError("缺少核心连接信息")

    stream = config.get("streamSettings", {})
    tls = stream.get("tlsSettings", {})
    ws = stream.get("wsSettings", {})
    grpc = stream.get("grpcSettings", {})
    network = stream.get("network", "tcp")

    params = {}
    if stream.get("security") == "tls":
        params["security"] = "tls"
        if sni := tls.get("serverName"):
            params["sni"] = sni
        if tls.get("allowInsecure") is not None:
            params["allowInsecure"] = "1" if tls.get("allowInsecure") else "0"
        if fp := tls.get("fingerprint"):
            params["fp"] = fp
        params["alpn"] = "http/1.1"

    params["type"] = network
    if network == "ws":
        if path := ws.get("path"):
            params["path"] = path
        params["host"] = tls.get("serverName", "vpn-node.internal")
    elif network == "grpc" and (svc := grpc.get("serviceName")):
        params["serviceName"] = svc

    params = {k: v for k, v in params.items() if v not in (None, "")}
    query = urllib.parse.urlencode(params)
    remark = urllib.parse.quote(node_name)
    return f"trojan://{password}@{address}:{port}?{query}#{remark}"

# ==================== 节点提取（两轮 + 失败诊断） ====================
def fetch_node_list(token: str) -> List[Dict]:
    headers = dict(COMMON_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    headers["Content-Type"] = "application/json"

    for api in CONFIG["api_servers"]:
        try:
            url = f"{api}/nodeList?platform=android"
            resp = get_session().post(url, headers=headers, json={},
                                      timeout=CONFIG.get("login_timeout", 12))
            if resp.status_code == 200:
                nodes = resp.json()
                if isinstance(nodes, list):
                    print(f"[+] 获取到 {len(nodes)} 个节点")
                    return nodes
        except Exception:
            continue
    raise Exception("所有节点 API 服务器均无法连接")

def fetch_client_config(node_id: int, token: str,
                        timeout_override: Optional[int] = None) -> Tuple[Optional[Dict], str]:
    """获取节点配置。返回 (配置字典或 None, 失败原因)。

    失败原因: ok / unavailable / timeout / http_xxx / error
    """
    headers = dict(COMMON_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    headers["Content-Type"] = "application/json"
    timeout = timeout_override or CONFIG.get("timeout", 5)

    last_reason = "error"
    for api in CONFIG["api_servers"]:
        try:
            url = f"{api}/clientConfig"
            resp = get_session().post(url, headers=headers,
                                      json={"nodeId": node_id, "ipv6": False},
                                      timeout=timeout)
            if resp.status_code == 409:
                return None, "unavailable"
            if resp.status_code == 200:
                return resp.json(), "ok"
            last_reason = f"http_{resp.status_code}"
        except requests.exceptions.Timeout:
            last_reason = "timeout"
        except Exception:
            last_reason = "error"
    return None, last_reason

def append_link_to_partial(link: str):
    """边提取边写文件，超时也不丢"""
    try:
        with open(PARTIAL_FILE, "a", encoding="utf-8") as f:
            f.write(link + "\n")
    except Exception:
        pass

def extract_single_node(node: Dict, token: str,
                        timeout_override: Optional[int] = None) -> Optional[Dict]:
    """提取单个节点。成功返回带 vlink 的结果；失败返回带 failed 标记的结果。"""
    node_id = node.get("nodeId")
    if not node_id:
        return None

    config, reason = fetch_client_config(node_id, token, timeout_override)
    if config is None:
        return {"failed": True, "reason": reason, "node_id": node_id}

    protocol = config.get("protocol", "").lower()
    name_cn = node.get("nameCn") or node.get("nameEn") or f"Node-{node_id}"
    country = get_country_from_name(name_cn)
    emoji = REGION_FLAGS.get(country, "🌐")
    display_name = f"{emoji} {name_cn}"

    link = None
    try:
        if protocol == "vless":
            link = generate_vless_link(config, display_name)
        elif protocol == "trojan":
            link = generate_trojan_link(config, display_name)
        else:
            return {"skipped": True, "protocol": protocol, "node_id": node_id}
    except Exception:
        link = None

    if link:
        append_link_to_partial(link)
        core_name, seq_num = parse_node_name_for_sort(name_cn)
        return {"vlink": link, "country": country,
                "core_name": core_name, "seq_num": seq_num, "node_id": node_id}
    return {"failed": True, "reason": "link_gen_failed", "node_id": node_id}

def extract_all_nodes(token: str) -> Tuple[str, int, int]:
    """两轮提取：首轮快速扫描，补漏轮用更长超时重试失败节点。返回 (内容, 成功数, 跳过数)"""
    nodes = fetch_node_list(token)
    if not nodes:
        raise Exception("节点列表为空")

    # 清空部分结果文件
    try:
        if os.path.exists(PARTIAL_FILE):
            os.remove(PARTIAL_FILE)
    except Exception:
        pass

    print(f"[-] 第一轮提取（{CONFIG['max_workers']} 线程，超时 {CONFIG['timeout']}s）...")
    results: List[Dict] = []
    skipped = 0
    failed_nodes: List[Tuple[int, str]] = []

    def run_round(node_subset: List[Dict], max_workers: int,
                  timeout_override: Optional[int]) -> Tuple[List[Dict], List[Tuple[int, str]]]:
        nonlocal skipped
        round_results = []
        round_failed = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_node = {executor.submit(extract_single_node, node, token, timeout_override): node
                              for node in node_subset}
            done = 0
            total = len(node_subset)
            for future in concurrent.futures.as_completed(future_to_node):
                done += 1
                res = future.result()
                if res:
                    if res.get("skipped"):
                        skipped += 1
                    elif res.get("failed"):
                        round_failed.append((res.get("node_id", 0), res.get("reason", "unknown")))
                    else:
                        round_results.append(res)
                else:
                    node = future_to_node[future]
                    round_failed.append((node.get("nodeId", 0), "no_result"))
                if done % 20 == 0 or done == total:
                    print(f"  进度: {done}/{total}")
        return round_results, round_failed

    # 第一轮
    results, failed_nodes = run_round(nodes, CONFIG["max_workers"], CONFIG["timeout"])
    print(f"  第一轮: 成功 {len(results)}，失败 {len(failed_nodes)}，跳过 {skipped}")

    final_failed_count = len(failed_nodes)

    # 第二轮补漏
    if failed_nodes:
        print(f"[-] 第二轮补漏（{CONFIG['retry_workers']} 线程，超时 {CONFIG['retry_timeout']}s）...")
        node_map = {n.get("nodeId"): n for n in nodes}
        retry_nodes = [node_map[fid] for fid, _ in failed_nodes if fid in node_map]

        retry_results, retry_failed = run_round(retry_nodes, CONFIG["retry_workers"],
                                                CONFIG["retry_timeout"])
        print(f"  第二轮: 成功 {len(retry_results)}，仍失败 {len(retry_failed)}")
        results.extend(retry_results)
        final_failed_count = len(retry_failed)

        # 汇总失败原因
        reason_counter: Dict[str, int] = {}
        for _, reason in retry_failed:
            reason_counter[reason] = reason_counter.get(reason, 0) + 1
        if reason_counter:
            print(f"  失败原因分布: {reason_counter}")

    # 排序
    sort_map = {name: i for i, name in enumerate(CONFIG["region_sort_order"])}
    results.sort(key=lambda x: (sort_map.get(x["country"], 999),
                                x["core_name"], x["seq_num"]))

    links = [r["vlink"] for r in results if r.get("vlink")]
    content = "\n".join(links)
    print(f"[+] 最终提取 {len(links)} 个节点，跳过 {skipped} 个，失败 {final_failed_count} 个")
    return content, len(links), skipped

# ==================== 主流程 ====================
def save_links(content: str):
    os.makedirs(os.path.dirname(os.path.abspath(OUTPUT_FILE)), exist_ok=True)
    header = f"# 火种VPN 节点订阅 - 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(header + content)
    try:
        if os.path.exists(PARTIAL_FILE):
            os.remove(PARTIAL_FILE)
    except Exception:
        pass
    print(f"\n{'=' * 60}")
    print(f"[OK] 已保存 {len(content.splitlines())} 个节点到:")
    print(f"     {OUTPUT_FILE}")
    print(f"{'=' * 60}")

def try_extract_with_token(token: str) -> bool:
    try:
        content, success, skipped = extract_all_nodes(token)
        if success > 0:
            save_links(content)
            return True
        print("[!] Token 有效但未提取到节点，可能不是 VIP")
        return False
    except Exception as e:
        print(f"[!] 使用缓存 Token 提取失败: {e}")
        return False

def mode_manual() -> bool:
    print("[*] 运行模式：手动（使用配置账号）")
    cached = get_cached_token()
    if cached:
        print("[*] 发现未过期缓存 Token，直接尝试提取...")
        if try_extract_with_token(cached):
            return True

    try:
        token = login(CONFIG["username"], CONFIG["password"])
    except Exception as e:
        print(f"[ERROR] 登录失败: {e}")
        return False

    return try_extract_with_token(token)

def mode_auto() -> bool:
    print("[*] 运行模式：全自动（注册 + 刷分 + 兑换 + 提取）")

    if not OCR_AVAILABLE:
        print("[ERROR] 自动模式需要 ddddocr，请先安装: pip install ddddocr")
        return False

    # 1. 优先使用缓存 Token
    cached = get_cached_token()
    if cached:
        print("[*] 发现未过期缓存 Token，直接尝试提取...")
        if try_extract_with_token(cached):
            return True
        print("[!] 缓存 Token 无效，继续走注册流程")

    # 2. 尝试复用已保存账号
    saved = load_saved_account()
    main_token = None
    if saved:
        print("[*] 发现已保存账号，尝试登录...")
        try:
            main_token = login(saved["username"], saved["password"])
            if check_vip_status(main_token):
                print("[+] 已保存账号仍为 VIP，直接提取节点")
            else:
                main_token = None
                print("[!] 已保存账号不是 VIP，重新注册")
        except Exception:
            main_token = None
            print("[!] 已保存账号登录失败，重新注册")

    # 3. 注册新主号
    if not main_token:
        clear_token_cache()
        main_acc = register_account(is_main=True)
        if not main_acc:
            print("[ERROR] 主号注册失败")
            return False
        save_account(main_acc)
        try:
            main_token = login(main_acc["username"], main_acc["password"])
        except Exception as e:
            print(f"[ERROR] 主号登录失败: {e}")
            return False

        try:
            ref_code = get_referral_code(main_token)
        except Exception as e:
            print(f"[ERROR] {e}")
            return False

        print(f"\n[-] 创建 {CONFIG['sub_account_count']} 个小号刷积分...")
        for i in range(CONFIG["sub_account_count"]):
            sub_acc = register_account(is_main=False, ref_code=ref_code)
            if sub_acc:
                try:
                    sub_token = login(sub_acc["username"], sub_acc["password"])
                    submit_referral(sub_token, sub_acc["deviceId"], ref_code)
                except Exception:
                    pass
            delay = random.uniform(CONFIG["register_delay_min"],
                                   CONFIG["register_delay_max"])
            if delay > 0:
                print(f"  冷却 {delay:.1f}s...")
                time.sleep(delay)

        print("\n[-] 尝试兑换 3 天 VIP...")
        try:
            redeem_vip(main_token)
        except Exception as e:
            print(f"[ERROR] {e}")
            return False
        time.sleep(1)

    # 4. 检查 VIP
    if not check_vip_status(main_token):
        print("[!] 当前账号不是 VIP，无法提取节点")
        return False

    # 5. 提取节点
    return try_extract_with_token(main_token)

def main():
    print("=" * 60)
    print("   火种VPN 一体化节点工具（两轮提取版）")
    print("=" * 60)
    print(f"输出路径: {OUTPUT_FILE}")
    print(f"OCR 可用: {'是' if OCR_AVAILABLE else '否'}")
    print(f"首轮: {CONFIG['max_workers']} 线程 / {CONFIG['timeout']}s 超时 | "
          f"补漏: {CONFIG['retry_workers']} 线程 / {CONFIG['retry_timeout']}s 超时\n")

    if CONFIG.get("username") and CONFIG.get("password"):
        success = mode_manual()
    else:
        success = mode_auto()

    if not success:
        if os.path.exists(PARTIAL_FILE):
            with open(PARTIAL_FILE, "r", encoding="utf-8") as f:
                partial_count = len(f.read().splitlines())
            if partial_count > 0:
                print(f"\n[INFO] 超时前已提取 {partial_count} 个节点（未排序），保存在:")
                print(f"       {PARTIAL_FILE}")
                print("[INFO] 下次运行会自动复用 Token 继续提取")
        print("\n[ERROR] 流程未完成")
        sys.exit(1)

if __name__ == "__main__":
    main()