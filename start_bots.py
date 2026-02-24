#!/usr/bin/env python3
"""
同时启动两个Kook机器人脚本
"""

import subprocess
import sys
import os
import threading
import datetime
import logging

def setup_logging():
    """设置日志记录，按日期生成日志文件"""
    # 创建log文件夹
    log_dir = "log"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # 生成按日期命名的日志文件名
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(log_dir, f"kook_bots_{today}.log")
    
    # 配置日志记录器
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    
    return logging.getLogger(__name__)

# 初始化日志记录器
logger = setup_logging()

def read_output(script_name, pipe):
    """实时读取并显示脚本输出，精简控制台输出，完整日志写入文件"""
    for line in iter(pipe.readline, ''):
        stripped_line = line.rstrip()
        # 将完整日志写入文件
        logger.info(f"[{script_name}] {stripped_line}")
        # 控制台只显示关键信息（如启动、停止、错误等）
        if any(keyword in stripped_line for keyword in ["启动", "停止", "错误", "Error", "ERROR", "成功", "Success", "SUCCESS"]):
            print(f"[{script_name}] {stripped_line}")
    pipe.close()

def start_bot(script_name):
    """启动指定的机器人脚本"""
    print(f"正在启动 {script_name}...")
    logger.info(f"开始启动机器人脚本: {script_name}")
    
    # 获取当前Python解释器路径
    python_path = sys.executable
    logger.debug(f"使用Python解释器: {python_path}")
    
    # 构建命令
    cmd = [python_path, script_name]
    logger.debug(f"执行命令: {' '.join(cmd)}")
    
    # 使用subprocess启动脚本，stdout和stderr重定向到PIPE，便于查看输出
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=os.getcwd())
    
    # 创建线程来实时读取输出
    output_thread = threading.Thread(target=read_output, args=(script_name, proc.stdout), daemon=True)
    output_thread.start()
    logger.info(f"{script_name} 启动成功，进程ID: {proc.pid}")
    
    return proc

def main():
    """主函数，同时启动两个机器人脚本"""
    logger.info("========== Kook机器人管理脚本启动 ==========")
    
    # 脚本列表
    bot_scripts = [
        "kook_deepseek_bot.py",
        "kook_deepseek_bot_ljmm.py"
    ]
    logger.debug(f"机器人脚本列表: {bot_scripts}")
    
    # 启动所有脚本
    processes = []
    for script in bot_scripts:
        if os.path.exists(script):
            proc = start_bot(script)
            processes.append((script, proc))
        else:
            warning_msg = f"脚本 {script} 不存在，跳过启动"
            print(f"警告：{warning_msg}")
            logger.warning(warning_msg)
    
    if processes:
        print("\n所有机器人脚本已启动！")
        print("按 Ctrl+C 停止所有脚本...\n")
        logger.info(f"成功启动 {len(processes)} 个机器人脚本")
    else:
        print("\n没有机器人脚本被启动！")
        logger.warning("没有机器人脚本被启动")
        return
    
    try:
        # 等待所有进程结束
        for script, proc in processes:
            proc.wait()
    except KeyboardInterrupt:
        print("\n正在停止所有机器人脚本...")
        logger.info("接收到Ctrl+C，开始停止所有机器人脚本")
        
        # 终止所有进程
        for script, proc in processes:
            proc.terminate()
            print(f"{script} 已停止")
            logger.info(f"已停止 {script}，进程ID: {proc.pid}")
    
    print("\n所有机器人脚本已停止")
    logger.info("========== Kook机器人管理脚本退出 ==========")

if __name__ == "__main__":
    main()