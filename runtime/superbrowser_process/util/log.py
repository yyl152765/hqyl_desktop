import logging
import os
import sys
from logging.handlers import RotatingFileHandler


class SafeRotatingFileHandler(RotatingFileHandler):
    """兼容 Windows 网络盘上 tell() 偶发返回 EINVAL 的滚动日志。"""

    def shouldRollover(self, record):
        try:
            return super().shouldRollover(record)
        except OSError:
            if self.maxBytes <= 0:
                return False
            try:
                current_size = os.path.getsize(self.baseFilename)
                message_size = len((self.format(record) + self.terminator).encode(self.encoding or "utf-8"))
                return current_size + message_size >= self.maxBytes
            except OSError:
                # 日志可写优先；本次无法判断大小时跳过滚动，下一条继续尝试。
                return False


class EncodingSafeStreamHandler(logging.StreamHandler):
    """Keep a narrow Windows console encoding from breaking Unicode logs."""

    def emit(self, record):
        try:
            message = self.format(record)
            stream = self.stream
            encoding = getattr(stream, "encoding", None)
            if encoding:
                try:
                    message.encode(encoding)
                except UnicodeEncodeError:
                    message = message.encode(
                        encoding,
                        errors="backslashreplace",
                    ).decode(encoding)
            stream.write(message + self.terminator)
            self.flush()
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)


def setup_logger(logger_name: str,
                 log_file: str,
                 level: int = logging.DEBUG,
                 fmt: str = '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                 max_bytes: int = 10 * 1024 * 1024,
                 backup_count: int = 5):
    """
    设置一个日志记录器（logger）

    参数:
      logger_name: 日志记录器名称，便于区分不同模块使用的 logger
      log_file: 日志文件的路径
      level: 日志记录级别，默认使用 DEBUG，可根据需要设为 INFO、WARNING 等
      fmt: 日志格式
      max_bytes: 单个日志文件大小达到此值后自动切割，默认 10MB
      backup_count: 保留的日志备份文件数量

    返回:
      配置好的 logger 对象
    """
    # 创建 logger 对象，并设置日志记录级别
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    # 业务 logger 自己维护文件/界面 handler，避免继续传播到可能已经
    # 失效的 root console handler（PyInstaller windowed 模式没有 stderr）。
    logger.propagate = False

    # 避免重复添加 handler
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    # 创建 Formatter 对象，定义日志输出格式
    formatter = logging.Formatter(fmt)

    # console=False 的 PyInstaller 桌面程序中 sys.stderr/sys.stdout 为 None。
    # 此时创建 StreamHandler 会在每条日志上触发 NoneType.write。
    console_stream = sys.stderr or sys.stdout
    if console_stream is not None and callable(getattr(console_stream, "write", None)):
        console_handler = EncodingSafeStreamHandler(console_stream)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        logger.addHandler(console_handler)

    # 创建一个 RotatingFileHandler，日志文件超过 max_bytes 后自动切割保存旧日志
    # 如果指定的日志目录不存在，则创建
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)

    file_handler = SafeRotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8')
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    logger.addHandler(file_handler)

    return logger


# 使用示例
if __name__ == "__main__":
    # 配置日志
    logger = setup_logger("myLogger", "logs/application.log")

    # 写入不同级别的日志
    logger.debug("这是调试信息")
    logger.info("这是普通信息")
    logger.warning("这是警告信息")
    logger.error("这是错误信息")
    logger.critical("这是严重错误信息")
