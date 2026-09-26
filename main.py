"""Read Aloud - a floating button that reads selected text aloud.

Usage:  Read Aloud [--selftest RESULT.json]
"""
import sys


def main():
    selftest = None
    if "--selftest" in sys.argv:
        selftest = sys.argv[sys.argv.index("--selftest") + 1]
    if sys.platform == "darwin":
        from readaloud.ui_mac import MacApp
        MacApp(selftest=selftest).run()
    else:
        from readaloud.ui_win import WinApp
        WinApp(selftest=selftest).run()


if __name__ == "__main__":
    main()
