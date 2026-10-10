# Shreder

A GNOME desktop app for securely shredding files and wiping free space on Linux.

![icon](data/icons/hicolor/scalable/apps/io.github.shreder.Shreder.svg)

## Features

- **Command Center**: lifetime stats, one-click actions, drive overview and recent activity.
- **Shred Files**: drag and drop files and folders (or right-click → Scripts → *Shred with Shreder* in Files). Each file is overwritten pass by pass, including the slack at the end of its last block. It can then be read back to verify the overwrite. Its name is scrambled through several random renames, and then it is deleted.
- **Remembered Folders**: save folders you clean out often. Each one has its own method, *contents only* or *whole folder* mode, an *older than N days* filter, a hidden-files toggle and a **daily or weekly schedule**. Scheduled folders are shredded in the background by a systemd user timer, even when the app is closed.
- **Wipe Free Space**: fills every free block on a drive with your chosen pattern, then releases it. With administrator mode (pkexec) it also covers the root-reserved blocks. On SSDs it can run **TRIM** for you.
- **Privacy sweeps**: securely empty the Trash (including trash folders on other drives), shred the thumbnail cache, and clear the recent-files history.
- **Guides**: step-by-step recipes for common situations, explaining why each step matters and what order to do them in. They cover sensitive files on an SSD, sensitive files on a hard disk, files already deleted the normal way, everyday traces, keeping Downloads clean, USB sticks, disposing of a whole drive, and btrfs/ZFS snapshots. Guides are tailored to your drives, and **Run All Steps** queues the whole sequence behind a single confirmation (cancelling one step stops the rest). After a shred or wipe on an SSD, a toast offers the next step.
- **Activity**: a full history of every operation, listing any file that could not be processed and why.
- A job queue with live progress, speed and ETA. You can cancel at any point, and free-space fill files are always cleaned up, even after a crash.

## Methods

| Method | Passes | Pattern |
|---|---|---|
| Zero Fill | 1 | `0x00` |
| Random Data | 1 | CSPRNG (NIST 800-88 Clear equivalent) |
| **DoD 5220.22-M (E)** (default) | 3 | `0x00`, `0xFF`, random + verify |
| DoD 5220.22-M (ECE) | 7 | E, random, E |
| Bruce Schneier | 7 | `0xFF`, `0x00`, random ×5 |
| Peter Gutmann | 35 | random ×4, 27 MFM/RLL patterns, random ×4 |

## Install

```sh
./install.sh            # app menu entry, icon, ~/.local/bin/shreder{,-cli}, Files action
./uninstall.sh [--purge]
```

You need `python3-gi`, `gir1.2-gtk-4.0` and `gir1.2-adw-1` (libadwaita ≥ 1.7).

## Command line

```sh
shreder-cli shred ~/secret.pdf                  # asks you to type 'shred'
shreder-cli shred ~/Downloads --keep-root --older-than 30 -y
shreder-cli wipe-free ~ -m random
shreder-cli methods | folders | run-scheduled
```

## Safety

- System locations, your home folder itself, `~/.config`, `~/.local` and similar paths are refused. A mounted drive's root can only be emptied (contents only), never removed.
- Symlinks are removed without touching their targets. Device files, sockets and pipes are skipped. Shredding never crosses into another mounted filesystem.
- **Hard links**: a file's data can have several names, and some tools (`uv`, `cp -l`, backup programs, deduplicators) create them on purpose. Overwriting the data under one name destroys it under every other name. When a file shares its data with files outside what you selected, Shreder removes the name and keeps the data, says so in the confirmation and the history, and leaves it to you to shred the other copies if you want the data gone. The command line can override this with `--shred-shared`.

## Honest limits

Overwriting in place is effective on spinning hard drives. On **SSDs, NVMe and USB flash**, wear-leveling and over-provisioning mean old copies of data can survive in blocks the OS can no longer address. **Copy-on-write filesystems** (btrfs, ZFS) and snapshots keep old versions too. For those drives:

- shred the files, then run Wipe Free Space (one random pass is enough), then run TRIM;
- for whole-drive sanitization, use the drive's built-in secure erase (`nvme format -s1`, `hdparm --security-erase`), or full-disk encryption from day one.

## Tests

```sh
python3 -m unittest tests.test_engine -v
```
