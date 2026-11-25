import logging
import os
from logging.handlers import RotatingFileHandler


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

    # 避免重复添加 handler
    if logger.hasHandlers():
        logger.handlers.clear()

    # 创建 Formatter 对象，定义日志输出格式
    formatter = logging.Formatter(fmt)

    # 创建一个控制台处理器，把日志输出到控制台
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    logger.addHandler(console_handler)

    # 创建一个 RotatingFileHandler，日志文件超过 max_bytes 后自动切割保存旧日志
    # 如果指定的日志目录不存在，则创建
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)

    file_handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8')
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
