from version import __version__

import welearn_time
import welearn_accuracy
import welearn_flow

print("**********  Created By Avenshy & SSmJaE  **********")
print("                 刷完成Version:" + __version__)
print("         原项目作者github https://github.com/Avenshy")
print("         更新维护者github https://github.com/YZBRH")
print("                         https://github.com/lxbsdsg")
print("***************************************************\n")

if __name__ == '__main__':
    while True:
        selected = input("1.刷完成度正确度启动\n2.刷时长启动\n3.顺序刷取启动\n0.退出=>")
        if selected == "1":
            welearn_accuracy.welearn_accuracy_run()
        elif selected == "2":
            welearn_time.welearn_time_run()
        elif selected == "3":
            welearn_flow.welearn_flow_run()
        else:
            break
