import win32serviceutil
import win32service
import win32event
import socket
import subprocess
import os
import sys


class DjangoService(win32serviceutil.ServiceFramework):
    _svc_name_ = "ResearchManagementService"
    _svc_display_name_ = "Research Management Django Service"
    _svc_description_ = "科研课题管理系统服务"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        socket.setdefaulttimeout(60)
        self.process = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)
        if self.process:
            self.process.terminate()

    def SvcDoRun(self):
        import servicemanager
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )

        # Use the repository root (this file's directory) as the working dir
        repo_root = os.path.dirname(os.path.abspath(__file__))
        os.chdir(repo_root)

        python_exe = sys.executable
        if python_exe.lower().endswith("pythonservice.exe"):
            python_exe = os.path.join(sys.exec_prefix, "python.exe")

        log_path = os.path.join(os.getcwd(), "django_service.log")
        log_file = open(log_path, "a", encoding="utf-8")

        # Ensure bundled dependencies are available (e.g., waitress) without global install
        vendor_path = os.path.join(repo_root, "_vendor")
        env = os.environ.copy()
        if os.path.isdir(vendor_path):
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = vendor_path + (os.pathsep + existing if existing else "")

        self.process = subprocess.Popen(
            [
                python_exe,
                "-m",
                "waitress",
                "--listen=0.0.0.0:1027",
                "--threads=8",
                "project_manager.wsgi:application",
            ],
            stdout=log_file,
            stderr=log_file,
            env=env,
        )

        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(DjangoService)
