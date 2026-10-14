Casharks SQL Backup – Quick Setup

Made by Casharks. This tool automatically backs up your FiveM database.

What You Need
- **Python 3.11 or newer**
- MariaDB tools (`mariadb-dump`, `mariadb`, and `mariadb-binlog`) or the MySQL versions.
- **rclone (optional)** – Only needed if you want to save backups to a remote location.

You do not need to install anything with pip.

**Start the sqlbackup.cmd AFTER you finish everything below**

Quick and Easy Setup

Open `config.lua` and fill out the following lines:

- **Lines 7–16:** Only edit lines 8, 9, 10, and 12 if your database username or settings are different.
- **Lines 19–36:** Only edit line 20. Leave the rest as it is unless you need to change something.
- **Lines 38–45:** Only edit line 39.
- **Lines 47–57:** Only edit lines 48 and 50.
- **Lines 81–84:** Edit lines 82 and 83 to set your disk space settings. The more disk space you have available, the more backups you can keep.
- **Lines 98–111:** Edit lines 99–105 to add your Discord webhook URLs for backup logs and notifications.

**That's it!** Once you've filled in the required settings, save the config and you're ready to set up your backups. **MAKE SURE TO RESTART YOUR SERVER**   