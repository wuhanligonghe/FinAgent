"""Historical Task Frame replay was retired after removing Task Frame."""
import argparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_known_args()
    parser.error("Task Frame 已移除，旧字段回放评测已停用；历史报告仅代表旧实现。请运行当前宿主回归测试。")


if __name__ == "__main__":
    main()
