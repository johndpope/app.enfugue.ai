from __future__ import annotations

import re
import os
import shutil
import datetime

from pathlib import Path
from typing import Dict, Any, Tuple, List, TypedDict, cast

from sqlalchemy import delete

from webob import Request, Response

from pibble.api.server.webservice.jsonapi import JSONWebServiceAPIServer
from pibble.api.exceptions import BadRequestError, NotFoundError, ConfigurationError
from pibble.ext.user.database import User
from pibble.ext.user.server.base import UserExtensionHandlerRegistry
from pibble.util.strings import Serializer
from pibble.util.encryption import Password

from enfugue.util import logger
from enfugue.api.controller.base import EnfugueAPIControllerBase

__all__ = ["EnfugueAPISystemController"]

LOG_REGEX = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}\ \d{2}:\d{2}:\d{2},\d+)\ \[(?P<logger>[a-zA-Z0-9_\.]+)\]\ (?P<level>[A-Z]+)\ \((?P<file>[^:]+):(?P<line>[\d]+)\)\ (?P<content>.*)$"
)


class LogDict(TypedDict):
    timestamp: datetime.datetime
    logger: str
    level: str
    file: str
    line: int
    content: str


def get_directory_size(directory: str, recurse: bool = True) -> Tuple[int, int, int]:
    """
    Sums the files and filesize of a directory
    """
    if not os.path.exists(directory):
        return 0, 0, 0
    items = os.listdir(directory)
    top_level_items = len(items)
    files, size = 0, 0
    for item in items:
        path = os.path.join(directory, item)
        if os.path.isfile(path):
            files += 1
            size += os.path.getsize(path)
        elif recurse:
            sub_items, sub_files, sub_size = get_directory_size(path, recurse=True)
            files += sub_files
            size += sub_size
    return top_level_items, files, size


class EnfugueAPISystemController(EnfugueAPIControllerBase):
    handlers = UserExtensionHandlerRegistry()

    @handlers.path("^/api/settings$")
    @handlers.methods("GET")
    @handlers.format()
    @handlers.secured("System", "read")
    def get_settings(self, request: Request, response: Response) -> Dict[str, Any]:
        """
        Gets the settings that can be manipulated from the UI
        """
        return {
            "safe": self.configuration.get("enfugue.safe", True),
            "auth": not (self.configuration.get("enfugue.noauth", True)),
            "max_queued_invocations": self.manager.max_queued_invocations,
            "max_queued_downloads": self.manager.max_queued_downloads,
            "max_concurrent_downloads": self.manager.max_concurrent_downloads,
            "switch_mode": self.configuration.get("enfugue.pipeline.switch", "offload"),
            "cache_mode": self.configuration.get("enfugue.pipeline.cache", "xl"),
            "precision": self.configuration.get("enfugue.dtype", None),
        }

    @handlers.path("^/api/settings$")
    @handlers.methods("POST")
    @handlers.format()
    @handlers.secured("System", "update")
    def update_settings(self, request: Request, response: Response) -> None:
        """
        Updates the settings that can be manipulated from the UI
        """
        if "auth" in request.parsed:
            self.user_config["enfugue.noauth"] = not request.parsed["auth"]
            if self.user_config["enfugue.noauth"] != request.parsed["auth"]:
                self.database.execute(delete(self.orm.AuthenticationToken))  # Clear auth data
                if request.parsed["auth"]:
                    self.database.execute(delete(self.orm.User).filter(self.orm.User.username == "noauth"))
                self.database.commit()
        if "safe" in request.parsed:
            self.user_config["enfugue.safe"] = request.parsed["safe"]
            self.manager.stop_engine()
        if "switch_mode" in request.parsed:
            if not request.parsed["switch_mode"]:
                self.user_config["enfugue.pipeline.switch"] = None
            else:
                self.user_config["enfugue.pipeline.switch"] = request.parsed["switch_mode"]
            self.manager.stop_engine()
        if "cache_mode" in request.parsed:
            if not request.parsed["cache_mode"]:
                self.user_config["enfugue.pipeline.cache"] = None
            else:
                self.user_config["enfugue.pipeline.cache"] = request.parsed["cache_mode"]
            self.manager.stop_engine()
        if "precision" in request.parsed:
            if not request.parsed["precision"]:
                self.user_config["enfugue.dtype"] = None
            else:
                self.user_config["enfugue.dtype"] = request.parsed["precision"]
            self.manager.stop_engine()
        for key in [
            "max_queued_invocation",
            "max_queued_downloads",
            "max_concurrent_downloads",
        ]:
            if key in request.parsed:
                self.user_config[f"enfugue.{key}"] = request.parsed[key]
        self.configuration.update(**self.user_config.dict())

    @handlers.path("^/api/users$")
    @handlers.methods("POST")
    @handlers.format()
    @handlers.secured("User", "create")
    def create_user(self, request: Request, response: Response) -> User:
        """
        Creates a user.
        """
        username = request.parsed.get("username", None)
        if not username:
            raise BadRequestError("Username is required.")
        user = self.database.query(self.orm.User).filter(self.orm.User.username == username).one_or_none()
        if user:
            raise BadRequestError(f"User {username} already exists.")
        password = request.parsed.get("new_password", None)
        repeat_password = request.parsed.get("repeat_password", None)

        if not password or not repeat_password:
            raise BadRequestError("Password is required.")
        if password != repeat_password:
            raise BadRequestError("Passwords do not match.")

        user = self.orm.User(username=username, password=Password.hash(password))

        if "first_name" in request.parsed:
            user.first_name = request.parsed["first_name"]
        if "last_name" in request.parsed:
            user.last_name = request.parsed["last_name"]

        self.database.add(user)
        self.database.commit()

        if "admin" in request.parsed and request.parsed["admin"]:
            admin_permission_group = (
                self.database.query(self.orm.PermissionGroup)
                .filter(self.orm.PermissionGroup.label == "admin")
                .one_or_none()
            )
            if not admin_permission_group:
                raise ConfigurationError(
                    "Couldn't find admin permission group. Did you modify the user initialization configuration?"
                )
            self.database.add(self.orm.UserPermissionGroup(user_id=user.id, group_id=admin_permission_group.id))
        self.database.commit()
        return user

    @handlers.path("^/api/users/(?P<username>[a-zA-Z0-9_]+)$")
    @handlers.methods("PATCH")
    @handlers.format()
    @handlers.secured("User", "update")
    def update_user(self, request: Request, response: Response, username: str) -> User:
        """
        Updates one user.
        """
        user = self.database.query(self.orm.User).filter(self.orm.User.username == username).one_or_none()
        if not user:
            raise NotFoundError(f"No user named {username}")

        if "first_name" in request.parsed:
            user.first_name = request.parsed["first_name"]
        if "last_name" in request.parsed:
            user.last_name = request.parsed["last_name"]
        if "new_password" in request.parsed and "repeat_password" in request.parsed:
            if request.parsed["new_password"] != request.parsed["repeat_password"]:
                raise BadRequestError("Passwords do not match.")
            user.password = Password.hash(request.parsed["new_password"])
        if "admin" in request.parsed:
            if username == "enfugue" and not request.parsed["admin"]:
                raise BadRequestError("Cannot demote default user.")

            admin_permission_group = (
                self.database.query(self.orm.PermissionGroup)
                .filter(self.orm.PermissionGroup.label == "admin")
                .one_or_none()
            )
            if not admin_permission_group:
                raise ConfigurationError(
                    "Couldn't find admin permission group. Did you modify the user initialization configuration?"
                )
            admin_permission = None
            for user_permission_group in user.permission_groups:
                if user_permission_group.group_id == admin_permission_group.id:
                    admin_permission = user_permission_group
                    break

            if admin_permission is not None and not request.parsed["admin"]:
                self.database.delete(admin_permission)
            elif admin_permission is None and request.parsed["admin"]:
                self.database.add(self.orm.UserPermissionGroup(user_id=user.id, group_id=admin_permission_group.id))
        self.database.commit()
        return user

    @handlers.path("^/api/users/(?P<username>[a-zA-Z0-9_]+)$")
    @handlers.methods("DELETE")
    @handlers.format()
    @handlers.secured("User", "delete")
    def delete_user(self, request: Request, response: Response, username: str) -> None:
        """
        Deletes one user.
        We have to do the cascading ourselves because of a bug with sqlite and sqlalchemy.
        """
        if username == "enfugue":
            raise BadRequestError("Cannot delete default user.")
        user = self.database.query(self.orm.User).filter(self.orm.User.username == username).one_or_none()
        if not user:
            raise NotFoundError(f"No user named {username}")
        for permission in user.permissions:
            self.database.delete(permission)
        for permission_group in user.permission_groups:
            self.database.delete(permission_group)
        self.database.commit()
        self.database.delete(user)
        self.database.commit()

    @handlers.path("^/api/installation$")
    @handlers.methods("GET")
    @handlers.format()
    @handlers.secured("System", "read")
    def get_installation_summary(self, request: Request, response: Response) -> Dict[str, Any]:
        """
        Gets a summary of files and filesize in the installation
        """
        sizes = {}
        for dirname in ["cache", "diffusers", "checkpoint", "lora", "lycoris", "inversion", "tensorrt", "other"]:
            directory = self.configuration.get(f"enfugue.engine.{dirname}", os.path.join(self.engine_root, dirname))
            items, files, size = get_directory_size(directory)
            sizes[dirname] = {"items": items, "files": files, "bytes": size, "path": directory}
        return sizes

    @handlers.path("^/api/installation$")
    @handlers.methods("POST")
    @handlers.format()
    @handlers.secured("System", "update")
    def change_installation_directories(self, request: Request, response: Response) -> None:
        """
        Changes all configured directories.
        """
        not_created = []
        for dirname in request.parsed["directories"]:
            path = request.parsed["directories"][dirname]
            exists = os.path.exists(path)
            if not exists:
                if Path(path).is_relative_to(self.engine_root) or request.parsed.get("create", False):
                    os.makedirs(path)
                    exists = True
                else:
                    not_created.append(path)
            if exists:
                self.user_config[f"enfugue.engine.{dirname}"] = path  # Save config to database
                self.configuration[f"enfugue.engine.{dirname}"] = path  # Save config to memory
        if not_created:
            not_created = list(set(not_created)) # remove duplicates
            y_ies = "ies" if len(not_created) > 1 else "y"
            do_does = "do" if len(not_created) > 1 else "does"
            not_created = ", ".join(not_created) # type: ignore
            raise BadRequestError(f"Director{y_ies} {do_does} not exist: {not_created}")

    @handlers.path("^/api/installation/(?P<dirname>[a-zA-Z0-9_]+)$")
    @handlers.methods("GET")
    @handlers.format()
    @handlers.secured("System", "read")
    def get_installation_details(self, request: Request, response: Response, dirname: str) -> List[Dict[str, Any]]:
        """
        Gets a summary of files and filesize in the installation
        """
        directory = self.configuration.get(f"enfugue.engine.{dirname}", os.path.join(self.engine_root, dirname))
        if not os.path.isdir(directory):
            return []
        items = []
        for item in os.listdir(directory):
            path = os.path.join(directory, item)
            if os.path.isdir(path):
                sub_items, files, size = get_directory_size(path)
                items.append({"type": "directory", "name": item, "bytes": size})
            else:
                items.append({"type": "file", "name": item, "bytes": os.path.getsize(path)})
        return items

    @handlers.path("^/api/installation/(?P<dirname>[^\/]+)/(?P<filename>[^\/]+)$")
    @handlers.methods("DELETE")
    @handlers.format()
    @handlers.secured("System", "update")
    def remove_from_installation(self, request: Request, response: Response, dirname: str, filename: str) -> None:
        """
        Deletes a file or directory from the installation
        """
        directory = self.configuration.get(f"enfugue.engine.{dirname}", os.path.join(self.engine_root, dirname))
        path = os.path.join(directory, filename)
        if not os.path.exists(path):
            raise BadRequestError(f"Unknown engine file/directory {dirname}/{filename}")
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)

    @handlers.bypass(JSONWebServiceAPIServer)
    @handlers.path("^/api/installation/(?P<dirname>[^\/]+)$")
    @handlers.methods("POST")
    @handlers.format()
    @handlers.secured("System", "update")
    def add_to_installation(self, request: Request, response: Response, dirname: str) -> None:
        """
        Uploads a file to an installation directory.
        """
        if "file" not in request.POST:
            raise BadRequestError("File is missing.")

        filename = request.POST["file"].filename
        directory = self.configuration.get(f"enfugue.engine.{dirname}", os.path.join(self.engine_root, dirname))
        if not os.path.exists(directory):
            raise BadRequestError(f"Unknown directory {dirname}")

        path = os.path.join(directory, filename)
        with open(path, "wb") as handle:
            for chunk in request.POST["file"].file:
                handle.write(chunk)

    @handlers.path("^/api/installation/(?P<dirname>[a-zA-Z0-9_]+)/move$")
    @handlers.methods("POST")
    @handlers.format()
    @handlers.secured("System", "update")
    def change_installation_directory(self, request: Request, response: Response, dirname: str) -> None:
        """
        Changes the directory of a particular model folder.
        """
        path = os.path.realpath(os.path.abspath(request.parsed["directory"]))
        if not os.path.exists(path):
            if Path(path).is_relative_to(self.engine_root) or request.parsed.get("create", False):
                os.makedirs(path)
            else:
                raise BadRequestError(f"Couldn't find directory {path}")
        self.user_config[f"enfugue.engine.{dirname}"] = path  # Save config to database
        self.configuration[f"enfugue.engine.{dirname}"] = path  # Save config to memory

    @handlers.path("^/api/logs$")
    @handlers.methods("GET")
    @handlers.format()
    @handlers.secured() # No specific permission, logs are redacted
    def read_logs(self, request: Request, response: Response) -> List[LogDict]:
        """
        Reads the log file and returns requested logs.
        """
        handler = self.configuration.get("enfugue.engine.logging.handler", None)
        if handler != "file":
            return []
        file_path = self.configuration.get("enfugue.engine.logging.file", None)
        if not file_path:
            raise ConfigurationError(f"Configuration does not have engine file logging enabled.")
        if file_path.startswith("~"):
            file_path = os.path.expanduser(file_path)
        file_path = os.path.realpath(os.path.abspath(file_path))
        if not os.path.exists(file_path):
            return []
        with open(file_path, "r") as fp:
            lines = fp.readlines()
        logs = self.parse_logs(lines)

        since = request.params.get("since", None)
        level = request.params.getall("level")
        loggers = request.params.getall("logger")
        search = request.params.get("search", None)

        if since is not None:
            since = Serializer.deserialize(since)
            if not isinstance(since, datetime.date) and not isinstance(since, datetime.datetime):
                raise BadRequestError(f"Bad date/time format {request.params['since']}")

        def include_log(log: LogDict) -> bool:
            """
            Returns whether or not a log should be included based on criteria.
            """
            if since is not None and log["timestamp"] < since:
                return False
            if level and log["level"] not in level:
                return False
            if loggers and log["logger"] not in loggers:
                return False
            if search is not None and search.lower() not in log["content"].lower():
                return False
            return True

        logs = [log for log in logs if include_log(log)]

        logs.sort(key=lambda log: log["timestamp"])
        logs.reverse()

        return logs

    @staticmethod
    def parse_logs(lines: List[str]) -> List[LogDict]:
        """
        Parses each line, discarding failed parses
        """
        logs = []
        for line in lines:
            try:
                parsed_line = LOG_REGEX.match(line)
                if not parsed_line:
                    raise ValueError("Unknown log format.")

                parsed_dict = parsed_line.groupdict()
                timestamp = datetime.datetime.strptime(parsed_dict["timestamp"], "%Y-%m-%d %H:%M:%S,%f")

                logs.append(
                    cast(
                        LogDict,
                        {
                            "timestamp": timestamp,
                            "logger": parsed_dict["logger"],
                            "level": parsed_dict["level"],
                            "file": parsed_dict["file"],
                            "line": parsed_dict["line"],
                            "content": parsed_dict["content"],
                        },
                    )
                )
            except ValueError as ex:
                if logs:
                    logs[-1]["content"] += f"\n{line}"
        return logs
