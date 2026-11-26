from version import __version__
import welearn_time

print("**********  Created By Avenshy & SSmJaE  **********")
print("                 刷完成Version:" + __version__)
print("         原项目作者github https://github.com/Avenshy")
print("         更新维护者github https://github.com/YZBRH")
print("                         https://github.com/lxbsdsg")
print("***************************************************\n")

if __name__ == '__main__':
    while True:
        selected = input("1.刷学习时长并按需写入正确率\n0.退出=>")
        if selected == "1":
            welearn_time.welearn_time_run()
        else:
            break
