# AI
"""
现在，基于上述的功能做拓展，要求如下：

程序的参数改为两个路径，A路径和B路径

给出以下3类的文件（以绝对路径的形式）
1 只在A路径中存在的文件
2 只在B路径中存在的文件
3 A和B路径中都存在的文件

注意：不要比较文件内容，不要尝试读写任何文件，文件名称以及相对位置相同既可视为相同的文件
"""

import os
import sys

def list_files_in_directory(directory):
    # 获取目录中所有文件的绝对路径
    file_set = set()
    for root, dirs, files in os.walk(directory):
        for name in files:
            # 生成文件的相对路径（相对于原始给定的目录）
            relative_path = os.path.relpath(os.path.join(root, name), directory)
            # 将相对路径加入集合
            file_set.add(relative_path)
    return file_set

def compare_directories(path_a, path_b):
    # 转换为绝对路径
    abs_path_a = os.path.abspath(path_a)
    abs_path_b = os.path.abspath(path_b)

    # 获取两个目录下的文件列表
    files_a = list_files_in_directory(abs_path_a)
    files_b = list_files_in_directory(abs_path_b)

    # 只在A中存在的文件
    only_in_a = files_a - files_b
    # 只在B中存在的文件
    only_in_b = files_b - files_a
    # A和B中都存在的文件
    both_in_a_and_b = files_a & files_b

    # 输出结果
    print("Files only in A ({}):".format(path_a))
    for file in only_in_a:
        print(os.path.join(abs_path_a, file))

    print("\nFiles only in B ({}):".format(path_b))
    for file in only_in_b:
        print(os.path.join(abs_path_b, file))

    print("\nFiles in both A and B:")
    for file in both_in_a_and_b:
        print(os.path.join(abs_path_a, file))  # 从A的路径获取绝对路径

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python compare.py [path_A] [path_B]")
    else:
        compare_directories(sys.argv[1], sys.argv[2])
