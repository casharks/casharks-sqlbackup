"""SQL for a least-privilege backup account."""

from .engines import quote_ident, quote_literal


def _grant_db(name: str) -> str:
    return quote_ident(name.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%"))


def backup_account_sql(flavor: str, version: tuple[int, ...], user: str, host: str, databases: list[str],
                       binlog: bool, password: str) -> str:
    account = f"{quote_literal(user)}@{quote_literal(host)}"
    lines = [
        f"CREATE USER IF NOT EXISTS {account} IDENTIFIED BY {quote_literal(password)};",
        f"ALTER USER {account} IDENTIFIED BY {quote_literal(password)};",
    ]
    privileges = "SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES"
    if databases == ["*"]:
        lines.append(f"GRANT {privileges} ON *.* TO {account};")
    else:
        lines += [f"GRANT {privileges} ON {_grant_db(db)}.* TO {account};" for db in databases]
    if flavor == "mariadb":
        lines.append(f"GRANT SELECT ON `mysql`.`proc` TO {account};")
        if binlog:
            monitor = "BINLOG MONITOR" if version >= (10, 5, 2) else "REPLICATION CLIENT"
            lines.append(f"GRANT RELOAD, {monitor} ON *.* TO {account};")
    else:
        if version >= (8, 0, 20):
            lines.append(f"GRANT SHOW_ROUTINE ON *.* TO {account};")
        if binlog:
            lines.append(f"GRANT RELOAD, REPLICATION CLIENT ON *.* TO {account};")
    return "\n".join(lines) + "\n"
