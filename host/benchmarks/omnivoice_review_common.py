"""Shared resource plots for the local OmniVoice listening pages."""
import csv


def plot_resources(result, folder):
    graph = folder / f'{result["id"]}-resources.svg'
    if graph.exists():
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.style.use("dark_background")
    with (folder / f'{result["id"]}-resources.csv').open() as stream:
        samples = list(csv.DictReader(stream))
    seconds = [float(sample["seconds"]) for sample in samples]
    cpu_seconds, cpu, baseline = [], [], 0
    for i in range(1, len(samples)):
        elapsed = seconds[i] - seconds[baseline]
        if elapsed >= 1:
            cpu_seconds.append(seconds[i])
            cpu.append(100 * (float(samples[i]["cpu_seconds"]) - float(samples[baseline]["cpu_seconds"])) / elapsed)
            baseline = i
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(9, 3.8), sharex=True, layout="constrained")
    fig.patch.set_facecolor("#18222e")
    for axis in (top, bottom):
        axis.set_facecolor("#18222e")
        axis.grid(alpha=.18)
    top.plot(cpu_seconds, cpu, color="#80d8ff")
    top.set_ylabel("CPU %")
    top.axhline(600, color="white", alpha=.3, ls="--")
    bottom.plot(seconds, [float(sample["rss_mib"]) / 1024 for sample in samples], color="#a3e0b5")
    bottom.set_ylabel("RSS (GiB)")
    bottom.set_xlabel("Seconds since model load began")
    for axis in (top, bottom):
        axis.set_ylim(bottom=0)
    fig.savefig(graph, facecolor=fig.get_facecolor())
    fig.savefig(graph.with_suffix(".png"), facecolor=fig.get_facecolor(), dpi=150)
    plt.close(fig)
