"""Builders for synthetic samples, shared by the tests."""

from whyslow.sampler import FULL, ProcInfo, ProcSample, Sample, SystemSample


def system(ts: float, plugged: bool = False, cpu: float = 50.0, mem: float = 50.0,
           disk_read: float | None = 1.0) -> SystemSample:
    return SystemSample(
        ts=ts, interval=1.0, cpu_percent=cpu, cpu_count=8, load1=1.0, mem_total=8, mem_used=4,
        mem_available=4, mem_percent=mem, swap_used=0, disk_read_bps=disk_read, disk_write_bps=2.0,
        net_sent_bps=3.0, net_recv_bps=4.0, battery_percent=80.0, power_plugged=plugged, battery_secs_left=None,
    )


def proc(pid: int, app: str, create_time: float = 1000.0) -> ProcInfo:
    return ProcInfo(pid=pid, create_time=create_time, name=f"p{pid}", exe=None, app=app,
                    cmdline=None, username="me", visibility=FULL)


def sample(ts: float, procs: list[tuple[ProcInfo, float, int]], **system_kw) -> Sample:
    """procs: (info, cpu_percent, rss) triples for a complete 1-second tick."""
    return Sample(system(ts, **system_kw),
                  [ProcSample(info, cpu / 100, cpu, rss, None) for info, cpu, rss in procs], [], 0.0, True)
