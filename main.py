import welearn_time
import welearn_accuracy

print("**********  Created By Avenshy & SSmJaE  **********")
print("                 刷完成Version:0.4dev")
print("         原项目作者github https://github.com/Avenshy")
print("         更新维护者github https://github.com/YZBRH/Welearn_helper/")
print("***************************************************\n")

if __name__ == '__main__':
    while True:
        selected = input("1.刷完成度正确度启动\n2.刷时长启动\n0.退出=>")
        if selected == "1":
            welearn_accuracy.welearn_accuracy_run()
        elif selected == "2":
            welearn_time.welearn_time_run()
        else:
            break
