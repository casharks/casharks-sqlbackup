Config = {}

Config.general = {
    instance_name = "cash", -- Name for your server used in backup names and alerts.
}

Config.database = {
    engine = "mariadb", -- Database type you use (auto, mariadb or mysql). Leave on auto.
    host = "localhost", -- Your database host. Usually localhost.
    port = 3306, -- Your database port. Usually 3306.
    socket = "", -- Leave empty.
    user = "root", -- Your database username.
    credential = "none", -- "none" = no password. "store" = run: sqlbackup credentials set database.password
    tls = "default", -- Leave on default.
    client_bin_dir = "", -- Folder with mariadb-dump.exe. Leave empty and it finds it.
    connect_timeout = 10, -- Seconds to wait when connecting to the database.
}

Config.backup = {
    databases = { "qbcore_cf4680" }, -- Databases to back up. Example: { "my_database" }. Use { "*" } for all.
    exclude_databases = {}, -- Databases to skip when using { "*" }.
    exclude_tables = {}, -- Tables to never back up. Example: { "my_database.logs" }
    schema_only_tables = {}, -- Tables to save empty (structure only), like big log tables.
    directory = "backups", -- Folder where backups are saved.
    full_backup_folder = "fullserver", -- Folder inside the backup folder for full backups.
    name_format = "Date{date}-FullServer_Time{time}", -- Name of the full backup file.
    date_format = "%m-%d-%y", -- How the date looks. %m-%d-%y = 10-14-26
    time_format = "%H-%M-%S", -- How the time looks. %H-%M-%S = 15-42-07, %I-%M-%S%p = 03-42-07PM
    compression = "none", -- "none" = normal .sql file, "gzip" = smaller .sql.gz file.
    compression_level = 6, -- How hard to compress gzip files (1 fast - 9 small).
    low_priority = true, -- Back up slower so the game server does not lag.
    routines = true, -- Also save stored procedures and functions.
    events = true, -- Also save database events.
    triggers = true, -- Also save triggers.
    max_allowed_packet_mb = 512, -- Biggest row size allowed. Leave it.
}

Config.selected_tables = {
    tables = { "qbcore_cf4680.inventories", "qbcore_cf4680.players", "qbcore_cf4680.player_vehicles" }, -- Tables to also save on their own. Example: { "my_database.players" }
    folder = "selectedtablets", -- Folder inside the backup folder for these table copies.
    name_format = "Date{date}-{table}_Time{time}", -- Name of each table copy file.
    keep_last = 60, -- How many copies to keep per table.
    max_age_days = 90, -- Delete table copies older than this many days. 0 = never.
    min_keep = 3, -- Always keep at least this many copies per table.
}

Config.schedule = {
    interval_minutes = 480, -- Back up every this many minutes. 0 = off.
    only_when_server_running = true, -- Only do those timed backups while the server is on.
    daily_times = { "04:00" }, -- Daily backup times (24 hour). Example: { "04:00", "16:00" }
    on_server_start = true, -- Back up after the server starts.
    server_start_delay_seconds = 90, -- Seconds to wait after the server starts.
    on_server_stop = true, -- Back up when the server stops, crashes or restarts.
    min_minutes_between_event_backups = 10, -- Skip the start backup if one was just made.
    retry_failed_after_minutes = 15, -- Minutes to wait before trying again after a failed backup.
    on_service_start = false, -- Back up every time the backup program starts.
}

Config.server_watch = {
    enabled = true, -- Watch for the FiveM server starting and stopping.
    process_names = { "FXServer.exe", "FXServer" }, -- Name of the FiveM server program.
    command_line_contains = { "+exec" }, -- Leave it. Makes it work with txAdmin.
    poll_seconds = 5, -- How often to check if the server is running (seconds).
}

Config.retention = {
    keep_last = 48, -- Always keep this many newest backups.
    keep_daily = 14, -- Keep one backup per day for this many days.
    keep_weekly = 8, -- Keep one backup per week for this many weeks.
    keep_monthly = 6, -- Keep one backup per month for this many months.
    max_age_days = 180, -- Delete backups older than this many days. 0 = never.
    min_keep = 3, -- Always keep at least this many backups.
}

Config.retry = {
    attempts = 3, -- How many times to try a backup if it fails.
    delay_seconds = 20, -- Seconds to wait before trying again.
    backoff_factor = 2.0, -- Wait longer each time it tries again.
}

Config.disk = {
    min_free_mb = 1024, -- Do not back up if the disk has less than this much free space (MB).
    warn_free_mb = 5120, -- Warn me when free space gets below this (MB).
}

Config.verify = {
    after_backup = true, -- Check every backup after it is made.
}

Config.remote = { -- mainly for brayden
    enabled = false, -- Also copy backups to another place (another drive, NAS or cloud folder).
    type = "directory", -- "directory" = a folder, "rclone" = a cloud remote.
    path = "", -- Where to copy them. Example: "E:\\Backups"
    rclone_binary = "rclone", -- Only for rclone. Leave it.
    keep_last = 30, -- How many backups to keep there. 0 = keep all.
}

Config.notify = {
    enabled = true, -- Send Discord messages.
    format = "discord", -- Leave on discord.
    webhook_url = "", -- Your Discord webhook. All messages go here.
    success_webhook_url = "", -- Webhook for good backups. Empty = use the one above.
    failure_webhook_url = "", -- Webhook for errors. Empty = use the main one.
    warning_webhook_url = "", -- Webhook for warnings. Empty = use the main one.
    inactive_webhook_url = "", -- Webhook for the unused tables report. Empty = use the main one.
    on_success = true, -- Send a message when a backup works.
    on_failure = true, -- Send a message when something fails.
    on_warning = true, -- Send a message for warnings.
    repeat_error_after_minutes = 60, -- Do not send the same error again for this many minutes.
    timeout_seconds = 10, -- Seconds to wait for Discord before giving up.
}

Config.inactive_tables = {
    enabled = true, -- Send a report of tables that have not changed in a long time.
    inactive_after_days = 30, -- Days with no change before a table is reported.
    report_every_days = 7, -- How often to send the report (days).
    only_new = true, -- Only show a table in full the first time it is reported.
    include_empty_tables = true, -- Also report empty tables.
    exclude_databases = {}, -- Databases to leave out of the report.
    exclude_tables = {}, -- Tables to leave out. Example: { "my_database.logs_*" }
}

Config.logging = {
    directory = "logs", -- Folder for log files.
    level = "INFO", -- How much to log (INFO is fine).
    max_file_mb = 5, -- Start a new log file at this size (MB).
    keep_files = 10, -- How many old log files to keep.
}

Config.service = {
    mode = "logon", -- "logon" = starts when you sign in to Windows.
    task_name = "", -- Name in Task Scheduler. Empty = automatic.
    watchdog_minutes = 10, -- Restart the backup program within this many minutes if it stops.
}

Config.restore = {
    user = "", -- Database user for restoring. Empty = same as above.
    credential = "none", -- "prompt" = ask for the password each time. "none" = no password.
    refuse_while_server_running = true, -- Do not restore while the server is running (keeps data safe).
}

Config.pitr = {
    binlog_dir = "", -- Advanced. Leave empty.
}

Config.security = {
    restrict_permissions = true, -- Only your Windows account can open the config, logs and backups.
    dpapi_scope = "user", -- Leave on user.
}
