# AI
"""
使用python写一段代码，完成以下功能，尽可能少使用第三方库

给定一个路径作为参数，列出该路径下所有的文件项（给出其完成路径），在MacOS上运行

比如：python list.py /home/a/data/
输出：/home/a/data/1.txt ...
"""

import os
import sys

def list_files(path):
    # 转换为绝对路径
    abs_path = os.path.abspath(path)
    
    # 检查给定的绝对路径是否存在
    if not os.path.exists(abs_path):
        print("The specified path does not exist.")
        return

    # 遍历给定路径下的所有文件，只打印文件的完整绝对路径
    for root, dirs, files in os.walk(abs_path):
        for name in files:
            print(os.path.join(root, name))

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python list.py [path]")
    else:
        list_files(sys.argv[1])
