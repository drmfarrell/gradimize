# data/

Only synthetic sample data lives here (`sample/`, tracked in git, plus the
semester-guide template). Everything else in this directory is gitignored.

**The live database does not live here.** On the production server the
application reads `GRADIMIZE_DATA_DIR=/mnt/nvme/gradimize-data` (exported at
the top of `~/.bashrc`), which keeps live data on the NVMe data drive —
outside the git working tree, so no git operation can ever capture it.
Nightly backups go to `~/gradimize-backups` on the OS drive (separate
physical disk; see `crontab -l`).

If a `gradimize.db` ever appears in this directory, something was started
without that environment variable — check the shell it launched from.
