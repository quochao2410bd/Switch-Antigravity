import win32gui
import win32process
import pywinauto

def enum_win32():
    print("=== WIN32 ENUM WINDOWS ===")
    windows = []
    def callback(hwnd, extra):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            windows.append((hwnd, pid, cls, title))
        return True
    win32gui.EnumWindows(callback, None)
    
    print(f"Total visible windows: {len(windows)}")
    for hwnd, pid, cls, title in windows:
        if any(term in title.lower() or term in cls.lower() for term in ['antigravity', 'oppenheimer', 'chrome', 'electron', 't0']):
            print(f"  MATCH: HWND={hwnd}, PID={pid}, Class='{cls}', Title='{title}'")
        elif title:
            print(f"  Visible: HWND={hwnd}, PID={pid}, Class='{cls}', Title='{title}'")

if __name__ == '__main__':
    enum_win32()
