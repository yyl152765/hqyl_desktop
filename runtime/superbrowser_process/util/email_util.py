import email
import imaplib
import smtplib
from email.header import decode_header, make_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from typing import List, Dict


class Email163Client:
    IMAP_HOST_163 = "imap.163.com"
    IMAP_HOST_QIYE = "imaphz.qiye.163.com"
    SMTP_HOST_163 = "smtp.163.com"
    SMTP_HOST_QIYE = "smtphz.qiye.163.com"
    IMAP_PORT = 993
    SMTP_PORT = 465

    def __init__(self, username: str, auth_code: str, mail_type: str = '163', mailbox: str = 'INBOX', debug: bool = False):
        """
        username: 邮箱账号
        auth_code: 客户端授权码
        mail_type: '163' 或 'qiye' (企业邮箱)
        mailbox: IMAP 邮箱选择
        debug: imaplib debug 输出
        """
        self.username = username
        self.auth_code = auth_code
        self.mail_type = mail_type
        self.mailbox = mailbox
        self.debug = debug
        self.imap_conn: Optional[imaplib.IMAP4_SSL] = None
        self.smtp_conn: Optional[smtplib.SMTP_SSL] = None
        self.last_uid = 0  # 可持久化，记录上次拉取的最大 UID

    # -------------------- IMAP 连接 --------------------
    def connect_imap(self) -> bool:
        """连接 IMAP 服务器并登录，解决网易 ID 命令问题"""
        try:
            server = self.IMAP_HOST_163 if self.mail_type == '163' else self.IMAP_HOST_QIYE

            # 避免 IMAP 默认 ID 命令触发 Unsafe Login
            imaplib.Commands["ID"] = "AUTH"

            self.imap_conn = imaplib.IMAP4_SSL(server, self.IMAP_PORT)
            if self.debug:
                self.imap_conn.debug = 4

            # 登录
            self.imap_conn.login(self.username, self.auth_code)

            # 发送客户端信息（网易要求）
            args = ('name', 'py_mail', 'contact', self.username,
                    'version', '1.0', 'vendor', 'python_client')
            self.imap_conn._simple_command("ID", '("' + '" "'.join(args) + '")')

            # 选择邮箱
            typ, data = self.imap_conn.select(self.mailbox)
            if typ != "OK":
                raise RuntimeError(f"选择邮箱失败: {typ} {data}")

            return True
        except Exception as e:
            print(f"IMAP连接失败: {e}")
            return False

    def get_unread_count(self) -> int:
        """获取未读邮件数量"""
        if not self.imap_conn:
            raise Exception("请先连接IMAP服务器")
        typ, data = self.imap_conn.search(None, 'UNSEEN')
        return len(data[0].split()) if data and data[0] else 0

    # -------------------- SMTP 连接 --------------------
    def connect_smtp(self) -> bool:
        """连接 SMTP 服务器"""
        try:
            server = self.SMTP_HOST_163 if self.mail_type == '163' else self.SMTP_HOST_QIYE
            self.smtp_conn = smtplib.SMTP_SSL(server, self.SMTP_PORT)
            self.smtp_conn.login(self.username, self.auth_code)
            return True
        except Exception as e:
            print(f"SMTP连接失败: {e}")
            return False

    def send_mail(self, to_addrs, subject, content, is_html=False) -> bool:
        """发送邮件（支持 HTML 格式）"""
        if not self.smtp_conn:
            raise Exception("请先连接SMTP服务器")

        msg = MIMEMultipart()
        msg['From'] = self.username
        msg['To'] = ', '.join(to_addrs) if isinstance(to_addrs, list) else to_addrs
        msg['Subject'] = subject

        if is_html:
            msg.attach(MIMEText(content, 'html'))
        else:
            msg.attach(MIMEText(content, 'plain'))

        try:
            self.smtp_conn.sendmail(self.username, to_addrs, msg.as_string())
            return True
        except Exception as e:
            print(f"邮件发送失败: {e}")
            return False

    # -------------------- 关闭连接 --------------------
    def close(self):
        """关闭所有连接"""
        if self.smtp_conn:
            try:
                self.smtp_conn.quit()
            except Exception:
                pass
        if self.imap_conn:
            try:
                self.imap_conn.close()
            except Exception:
                pass
            try:
                self.imap_conn.logout()
            except Exception:
                pass


    def fetch_latest_emails(self, limit: int = 100, mark_seen: bool = False) -> List[Dict]:
        """
        获取最新 N 封邮件（不区分已读/未读）
        :param limit: 要获取的邮件数量，默认100
        :param mark_seen: 是否标记为已读
        :return: 列表，每封邮件是字典，包含 uid, from, sender, original_from, subject, date, body, otp
        """
        if not self.imap_conn:
            raise Exception("请先连接IMAP服务器")

        self.imap_conn.select(self.mailbox)

        # 搜索所有邮件
        typ, data = self.imap_conn.search(None, "ALL")
        if typ != 'OK' or not data or not data[0]:
            print("未找到邮件")
            return []


        # 获取邮件 UID 列表，并取最后 limit 封邮件（最新的）
        all_nums = data[0].split()
        latest_nums = all_nums[-limit:]

        email_list = []

        for num in latest_nums:
            typ, msg_data = self.imap_conn.fetch(num, '(RFC822)')
            if typ != 'OK':
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            # --- 主题解析 ---
            subject, encoding = decode_header(msg.get("Subject"))[0]
            if isinstance(subject, bytes):
                subject = subject.decode(encoding or 'utf-8', errors='ignore')

            # --- 发件人、代发源、原始发件人解析 ---
            from_ = msg.get("From")
            sender = msg.get("Sender")  # 代发邮箱
            received_list = msg.get_all("Received", [])

            # 尝试解析最原始发送邮箱
            original_from = None
            if received_list:
                for r in reversed(received_list):
                    match = re.search(r'from\s+<?([^>\s]+)>?', r)
                    if match:
                        original_from = match.group(1)
                        break

            # --- 日期转换为 yyyy-mm-dd HH:MM:SS ---
            date_ = msg.get("Date")
            formatted_date = date_
            try:
                dt = parsedate_to_datetime(date_)
                formatted_date = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

            # --- 正文解析 ---
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_dispo = str(part.get("Content-Disposition"))
                    if content_type in ["text/plain", "text/html"] and "attachment" not in content_dispo:
                        charset = part.get_content_charset() or 'utf-8'
                        body = part.get_payload(decode=True).decode(charset, errors='ignore')
                        break
            else:
                charset = msg.get_content_charset() or 'utf-8'
                body = msg.get_payload(decode=True).decode(charset, errors='ignore')

            # --- OTP 提取 ---
            otp = None
            if body:
                candidates = re.findall(r'(?<!\d)(\d{6})(?!\d)', body)
                if candidates:
                    keywords = ['otp', 'code', '验证码', 'verification']
                    body_lower = body.lower()
                    for cand in candidates:
                        idx = body_lower.find(cand)
                        if idx != -1:
                            window = body_lower[max(0, idx - 40): idx + 40]
                            if any(k in window for k in keywords):
                                otp = cand
                                break
                    if not otp:
                        otp = candidates[0]

            # --- 记录邮件 ---
            email_list.append({
                "uid": num.decode() if isinstance(num, bytes) else num,
                "from": from_,
                "sender": sender,
                "original_from": original_from,
                "subject": subject,
                "date": formatted_date,
                "body": body,
                "otp": otp
            })

            # 标记已读
            if mark_seen:
                self.imap_conn.store(num, '+FLAGS', '\\Seen')

        # 按日期降序排列（最新在前）
        email_list.sort(key=lambda e: e.get("date") or "", reverse=True)
        return email_list


    def fetch_latest_emails2(self, limit: int = 20, mark_seen: bool = False) -> List[Dict]:
        """
        高效拉取最新 N 封邮件
        """
        if not self.imap_conn:
            raise Exception("请先连接IMAP服务器")

        self.imap_conn.select(self.mailbox)

        # 只拉取比 last_uid 大的邮件
        search_criteria = f'UID {self.last_uid + 1}:*'
        typ, data = self.imap_conn.uid('search', None, search_criteria)
        if typ != 'OK' or not data or not data[0]:
            return []

        all_uids = data[0].split()
        if not all_uids:
            return []

        latest_uids = all_uids[-limit:]

        # 批量 fetch
        typ, fetched_data = self.imap_conn.uid('fetch', b','.join(latest_uids), '(RFC822)')
        if typ != 'OK':
            return []

        emails = []
        # 每封邮件数据在 fetched_data 中成对出现
        for i in range(0, len(fetched_data), 2):
            if len(fetched_data[i]) < 2:
                continue
            uid = latest_uids[i // 2]
            raw_email = fetched_data[i][1]

            try:
                msg = email.message_from_bytes(raw_email)
            except Exception:
                continue

            # --- 标题 ---
            try:
                subject = str(make_header(decode_header(msg.get("Subject", ""))))
            except:
                subject = msg.get("Subject", "")

            # --- 发件人 / 代发 / 原始发件人 ---
            from_ = msg.get("From", "")
            sender = msg.get("Sender", "")
            original_from = None
            for r in reversed(msg.get_all("Received", []) or []):
                match = re.search(r'from\s+<?([^>\s]+)>?', r)
                if match:
                    original_from = match.group(1)
                    break

            # --- 日期 ---
            date_ = msg.get("Date")
            formatted_date = date_
            try:
                dt = parsedate_to_datetime(date_)
                formatted_date = dt.strftime("%Y-%m-%d %H:%M:%S")
            except:
                pass

            # --- 正文 ---
            body = ""
            try:
                if msg.is_multipart():
                    for part in msg.walk():
                        ctype = part.get_content_type()
                        dispo = str(part.get("Content-Disposition") or "")
                        if ctype in ["text/plain", "text/html"] and "attachment" not in dispo:
                            charset = part.get_content_charset() or 'utf-8'
                            body = part.get_payload(decode=True).decode(charset, errors='ignore')
                            if body.strip():
                                break
                else:
                    charset = msg.get_content_charset() or 'utf-8'
                    body = msg.get_payload(decode=True).decode(charset, errors='ignore')
            except:
                pass

            # --- OTP 提取 ---
            otp = None
            has_otp = False
            if body:
                candidates = re.findall(r'(?<!\d)(\d{4,8})(?!\d)', body)
                if candidates:
                    keywords = ['otp', 'code', '验证码', 'verification', 'auth', 'passcode']
                    body_lower = body.lower()
                    for cand in candidates:
                        idx = body_lower.find(cand)
                        if idx != -1:
                            window = body_lower[max(0, idx - 40): idx + 40]
                            if any(k in window for k in keywords):
                                otp = cand
                                has_otp = True
                                break
                    if not otp:
                        otp = candidates[0]
                        has_otp = True

            emails.append({
                "uid": uid.decode() if isinstance(uid, bytes) else uid,
                "from": from_,
                "sender": sender,
                "original_from": original_from,
                "subject": subject,
                "date": formatted_date,
                "body": body,
                "otp": otp,
                "has_otp": has_otp
            })

            if mark_seen:
                self.imap_conn.uid('store', uid, '+FLAGS', '(\\Seen)')

        # 按日期降序排列
        emails.sort(key=lambda e: e.get("date") or "", reverse=True)

        # 更新 last_uid
        self.last_uid = max(self.last_uid, max(int(uid) for uid in latest_uids))

        return emails

import re
from typing import Optional
from bs4 import BeautifulSoup
def extract_otp(body: str) -> Optional[str]:
    """
    从邮件正文提取 6 位验证码：
    - 可以是纯数字或字母+数字混合
    - 必须至少包含一个数字
    - 优先匹配靠近关键字（code, otp, verification, 验证码, 确认码）
    - 避免全 0 或纯字母无效组合
    """

    if not body:
        return None

    # 去掉 HTML 标签
    text = BeautifulSoup(body, 'html.parser').get_text(separator=' ')
    text_upper = text.upper()

    # 关键字
    keywords = ['OTP', 'CODE', 'VERIFICATION', '验证码', '确认码']

    # 先在关键字附近找 OTP
    for kw in keywords:
        for match in re.finditer(kw, text_upper):
            start, end = match.start(), match.end()
            window = text_upper[max(0, start-50): end+50]
            # 匹配 6 位，至少含一个数字，不能是全 0
            otp_match = re.search(r'\b(?=.*\d)[A-Z0-9]{6}\b', window)
            if otp_match:
                otp = otp_match.group()
                if otp != '000000':
                    return otp

    # 全文找第一个有效 OTP（6 位，至少一个数字，不全 0）
    candidates = [
        m.group() for m in re.finditer(r'\b(?=.*\d)[A-Z0-9]{6}\b', text_upper)
        if m.group() != '000000'
    ]
    if candidates:
        return candidates[0]

    return None

def split_email(email):
    """
    将邮箱地址拆分为前缀和后缀
    返回一个包含 (前缀, 后缀) 的元组
    """
    if '@' not in email:
        raise ValueError(f"无效的邮箱地址: '{email}'")
    parts = email.split('@', 1) # 只分割一次，防止本地部分包含@（虽然标准不允许）
    prefix = parts[0]
    suffix = parts[1]
    return prefix, suffix

def extract_masked_email(text: str) -> str:
    """
    从弹窗文本中提取邮箱（包括带星号的格式，如 6******@xxx.com）
    """
    # 邮箱模式：前面可包含数字+星号组合
    pattern = r'([\w\.\-]+[*\w\.\-]*@[\w\.\-]+\.\w+)'
    match = re.search(pattern, text)
    return match.group(1) if match else None

def get_opt_code(username,auth_code,email_prefix,email_suffix,get_email_type):

    # 初始化邮箱工具
    mail = Email163Client(username=username,
                          auth_code=auth_code,
                          mail_type='163',
                          debug=True)
    if mail.connect_imap():
        print(f"未读邮件数: {mail.get_unread_count()}")
    otp_code = ""
    send_time =""
    emails = mail.fetch_latest_emails2(mark_seen=False)
    for e in emails:
        subject_lower = (e['subject'] or "").lower()
        if 'otp' in subject_lower and 'code' in subject_lower:
            print(subject_lower)
        from_lower = (e['from'] or "")
        to_lower = (e['from'] or "")
        sender_lower = (e['sender'] or "")

        if "转发：" in subject_lower or "fw:" in subject_lower or "fwd:" in subject_lower or "otp" in subject_lower or "验证码" in subject_lower or "code" in subject_lower:
            if get_email_type in e['body']:
                # if get_email_name in from_lower or get_email_name in to_lower or get_email_name in sender_lower:
                if (email_prefix in from_lower and email_suffix in from_lower) or (email_suffix in to_lower and email_suffix in to_lower) or (email_suffix in sender_lower and email_suffix in sender_lower):
                    # 解析为 datetime 对象
                    try:
                        # 尝试解析标准邮件格式
                        dt = parsedate_to_datetime(e['date'])
                        send_time = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        send_time = e['date']
                    otp_code  = extract_otp(e['body'])
                    if otp_code is not  None:
                        print(f"接收到{email_prefix}@{email_suffix}的转发邮件，提取到{get_email_type}验证码：{otp_code}, 时间：{send_time}")
                        break
    # 关闭连接
    mail.close()
    return otp_code,send_time


def extract_emails(text: str) -> List[str]:
    """
    从字符串中提取所有邮箱地址
    """
    if not text:
        return []

    # 匹配邮箱的正则
    pattern = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
    return re.findall(pattern, text)


def contains_keywords(subject: str, keyword_file = r"C:\rpa\验证码助手\config\过滤词.txt") -> bool:
    """
    判断邮件主题是否包含关键词。

    :param subject: 邮件主题（会自动转为小写）
    :param keyword_file: 存放关键词的txt文件路径，每行一个关键词
    :return: 若包含任意关键词则返回 True，否则 False
    """
    subject_lower = subject.lower().replace("：", ":")  # 统一中英文冒号
    try:
        with open(keyword_file, "r", encoding="utf-8") as f:
            keywords = [line.strip().lower() for line in f if line.strip()]  # 去除空行和首尾空格
    except FileNotFoundError:
        print(f"未找到关键字文件: {keyword_file}")
        return False

    return any(keyword in subject_lower for keyword in keywords)

if __name__ == '__main__':
    # username = '18571756471@163.com'
    # auth_code = 'NCfe8gvryeQB4jCH'
    username = ""
    auth_code = ""

    get_email_name = ""
    get_email_type = "Shopee"

    email_prefix, email_suffix = split_email(get_email_name)
    otp_code,send_time = get_opt_code(username,auth_code,email_prefix,email_suffix,get_email_type)
    print(f"{otp_code} == {send_time}")
