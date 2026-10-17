"""以脱离终端的守护进程方式启动作品服务。

用法：python start_server.py [端口]
停止：kill $(cat data/server.pid)

默认监听 0.0.0.0，同一局域网的手机可以直接用 http://<电脑IP>:<端口> 打开；
只想本机访问时设 PA_BIND=127.0.0.1。

pid 文件由守护进程自己写（父进程第一次 fork 出来的中间进程会随即退出，
打印它的 pid 没用）。写之前会先清掉指向已死进程的过期文件，
所以「服务已在运行」这句话只在真的有进程在听的时候出现。
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = Path("/tmp/pa_server.log")
PID = ROOT / "data" / "server.pid"


def lan_ip():
    """取本机局域网 IP；取不到就返回空串，由调用方说明。

    原先用 socket.connect(192.0.2.1) 取默认路由的出口地址，VPN 开着时
    默认路由指向 utun*，打印出来的手机地址是隧道地址，
    手机和电脑不在同一物理网络，扫码、手打都打不开——而地址格式完全正常，
    用户只会以为是自己输错。现在按 ifconfig 取真实 Wi-Fi/以太网地址，
    状态 active 的优先；VPN 地址只作最后兜底并标注。
    """
    manual = os.environ.get("PA_LAN_IP", "").strip()
    if manual:
        return manual
    import re
    import subprocess
    try:
        txt = subprocess.run(["ifconfig"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        txt = ""
    active, standby = [], []
    name = ""
    running = False
    for line in txt.splitlines():
        head = re.match(r"^([A-Za-z0-9]+): flags=\S+<([^>]*)>", line)
        if head:
            name = head.group(1)
            running = "RUNNING" in head.group(2).split(",")
            continue
        m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", line)
        if not m:
            continue
        # 只认真实物理接口，排除 utun*/awdl*/lo* 等虚拟接口。
        if not (name.startswith("en") or name.startswith("bridge")):
            continue
        (active if running else standby).append(m.group(1))
    for ip in active + standby:
        if not ip.startswith("127."):
            return ip
    # 兜底：默认路由地址。可能是 VPN，标注出来别让它冒充 Wi-Fi 地址。
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.4)
        sock.connect(("192.0.2.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def print_addrs(port):
    print("本机打开：  http://127.0.0.1:%s" % port)
    ip = lan_ip()
    if ip:
        print("手机打开：  http://%s:%s   （手机需与电脑连同一局域网）" % (ip, port))
    else:
        print("手机打开：  没取到局域网 IP，可在手机上试 http://<电脑IP>:%s" % port)


def _listening_port(pid):
    """这个 pid 实际在监听哪个端口。取不到就返回空串。

    已有服务在跑时，不能拿本次命令行传的端口号当成它的端口：
    传错的话打印出来的地址点不开，用户会以为服务没起来。
    """
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        # lsof 最后一列是 (LISTEN)，不是端口。端口在 NAME 列，即倒数第二列，
        # 形如「TCP 127.0.0.1:8000 (LISTEN)」。取最后一列永远得到 (LISTEN)，
        # 判断不了是不是数字，于是这个函数一直返回空串——看着像探测失败，
        # 其实是取错了列。
        name = parts[-2]
        tail = name.rsplit(":", 1)[-1]
        if tail.isdigit():
            return tail
    return ""


def main():
    # 没传端口时才用默认值：写成 len(sys.argv) 会在无参数时越界。
    port = sys.argv[1] if len(sys.argv) > 1 else "8000"
    if PID.exists():
        old = PID.read_text().strip()
        try:
            os.kill(int(old), 0)
            print("服务已在运行，pid=%s" % old)
            # 打印已跑服务的真实端口，不是本次传进来的那个。
            live = _listening_port(old)
            if live:
                print_addrs(live)
                if live != port:
                    print("注意：传的端口是 %s，但已在跑的服务监听的是 %s。" % (port, live))
            else:
                # 探测不到就明说端口未确认。拿本次传的端口顶上，
                # 等于把没验证过的数字当成事实打印出去，用户点开会打不开。
                print("本机打开：  http://127.0.0.1:<端口未确认>")
                print("端口没能从进程信息里确认，请用下面的命令查实际端口：")
                print("  lsof -nP -iTCP -sTCP:LISTEN -a -p %s" % old)
            return 0
        except (ValueError, ProcessLookupError, OSError):
            # 上次异常退出会留下一个指向已死进程的 pid 文件。不删掉的话，
            # 下次启动一直卡在「服务已在运行」，而那个进程根本不存在——
            # 用户看到的是一个打不开的地址。missing_ok 是防止并发启动时
            # 另一方已经删过，那会抛 FileNotFoundError 把启动整个带崩。
            PID.unlink(missing_ok=True)
            print("清掉了一份过期的 pid 文件（指向的进程已经不在了）")

    ROOT.joinpath("data").mkdir(exist_ok=True)
    bind = os.environ.get("PA_BIND", "0.0.0.0")
    log = open(LOG, "ab")
    # 真正监听端口的那个进程（第二次 fork 出来的守护进程）才知道自己的 pid，
    # 所以由它写文件，父进程从管道读回来打印。少了这一段，data/server.pid
    # 永远不存在，README 里的「停止：kill $(cat data/server.pid)」就是一句
    # 空话，用户拿着空文件没法停服务；父进程原先打印的还是第一次 fork
    # 出来的中间进程（它随即退出），照着 kill 会报没有这个进程。
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid > 0:
        os.close(write_fd)
        with os.fdopen(read_fd, "rb") as pipe:
            real = pipe.read().decode().strip()
        print("服务已启动，pid=%s" % (real or pid))
        print_addrs(port)
        print("日志：%s" % LOG)
        print("停止：kill %s" % (real or pid))
        return 0

    os.close(read_fd)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir(str(ROOT))
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    # 先落 pid 再拉起服务：父进程阻塞在管道读上，就等这个值打印启动信息。
    # 写在 uvicorn.run 之后的话，服务起不来时父进程也拿不到 pid，
    # 屏幕上只剩一个永远不出现的进程号，用户还是没法停服务。
    PID.write_text(str(os.getpid()))
    os.write(write_fd, str(os.getpid()).encode())
    os.close(write_fd)
    os.environ["PORT"] = port
    sys.path.insert(0, str(ROOT))
    import uvicorn
    uvicorn.run("app.main:app", host=bind, port=int(port), reload=False, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
