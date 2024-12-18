import welearn_time
import welearn_accuracy

if __name__ == '__main__':
    while True:
        selected = input("1.刷完成度正确度启动\n2.刷时长启动\n0.退出=>")
        if selected == "1":
            welearn_accuracy.welearn_accuracy_run()
        elif selected == "2":
            welearn_time.welearn_time_run()
        else:
            break
