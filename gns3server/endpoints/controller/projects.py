# -*- coding: utf-8 -*-
#
# Copyright (C) 2020 GNS3 Technologies Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
API endpoints for projects.
"""

import os
import asyncio
import tempfile
import zipfile
import aiofiles
import time

import logging
log = logging.getLogger()

from fastapi import APIRouter, Depends, Request, Body, Query, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse, FileResponse
from websockets.exceptions import ConnectionClosed, WebSocketException
from typing import List, Optional
from uuid import UUID
from pathlib import Path

from gns3server.endpoints.schemas.common import ErrorMessage
from gns3server.endpoints import schemas
from gns3server.controller import Controller
from gns3server.controller.project import Project
from gns3server.controller.controller_error import ControllerError, ControllerForbiddenError
from gns3server.controller.import_project import import_project as import_controller_project
from gns3server.controller.export_project import export_project as export_controller_project
from gns3server.utils.asyncio import aiozipstream
from gns3server.config import Config


router = APIRouter()

responses = {
    404: {"model": ErrorMessage, "description": "Could not find project"}
}


def dep_project(project_id: UUID):
    """
    Dependency to retrieve a project.
    """

    project = Controller.instance().get_project(str(project_id))
    return project


CHUNK_SIZE = 1024 * 8  # 8KB


@router.get("",
            response_model=List[schemas.Project],
            response_model_exclude_unset=True)
def get_projects():
    """
    Return all projects.
    """

    controller = Controller.instance()
    return [p.__json__() for p in controller.projects.values()]


@router.post("",
             status_code=status.HTTP_201_CREATED,
             response_model=schemas.Project,
             response_model_exclude_unset=True,
             responses={409: {"model": ErrorMessage, "description": "Could not create project"}})
async def create_project(project_data: schemas.ProjectCreate):
    """
    Create a new project.
    """

    controller = Controller.instance()
    project = await controller.add_project(**jsonable_encoder(project_data, exclude_unset=True))
    return project.__json__()


@router.get("/{project_id}",
            response_model=schemas.Project,
            responses=responses)
def get_project(project: Project = Depends(dep_project)):
    """
    Return a project.
    """

    return project.__json__()


@router.put("/{project_id}",
            response_model=schemas.Project,
            response_model_exclude_unset=True,
            responses=responses)
async def update_project(project_data: schemas.ProjectUpdate, project: Project = Depends(dep_project)):
    """
    Update a project.
    """

    await project.update(**jsonable_encoder(project_data, exclude_unset=True))
    return project.__json__()


@router.delete("/{project_id}",
               status_code=status.HTTP_204_NO_CONTENT,
               responses=responses)
async def delete_project(project: Project = Depends(dep_project)):
    """
    Delete a project.
    """

    controller = Controller.instance()
    await project.delete()
    controller.remove_project(project)


@router.get("/{project_id}/stats",
            responses=responses)
def get_project_stats(project: Project = Depends(dep_project)):
    """
    Return a project statistics.
    """

    return project.stats()


@router.post("/{project_id}/close",
             status_code=status.HTTP_204_NO_CONTENT,
             responses={
                 **responses,
                 409: {"model": ErrorMessage, "description": "Could not close project"}
             })
async def close_project(project: Project = Depends(dep_project)):
    """
    Close a project.
    """

    await project.close()


@router.post("/{project_id}/open",
             status_code=status.HTTP_201_CREATED,
             response_model=schemas.Project,
             responses={
                 **responses,
                 409: {"model": ErrorMessage, "description": "Could not open project"}
             })
async def open_project(project: Project = Depends(dep_project)):
    """
    Open a project.
    """

    await project.open()
    return project.__json__()


@router.post("/load",
             status_code=status.HTTP_201_CREATED,
             response_model=schemas.Project,
             responses={
                 **responses,
                 409: {"model": ErrorMessage, "description": "Could not load project"}
             })
async def load_project(path: str = Body(..., embed=True)):
    """
    Load a project (local server only).
    """

    controller = Controller.instance()
    config = Config.instance()
    dot_gns3_file = path
    if config.get_section_config("Server").getboolean("local", False) is False:
        log.error("Cannot load '{}' because the server has not been started with the '--local' parameter".format(dot_gns3_file))
        raise ControllerForbiddenError("Cannot load project when server is not local")
    project = await controller.load_project(dot_gns3_file,)
    return project.__json__()


@router.get("/{project_id}/notifications")
async def notification(project_id: UUID):
    """
    Receive project notifications about the controller from HTTP stream.
    """

    controller = Controller.instance()
    project = controller.get_project(str(project_id))

    log.info("New client has connected to the notification stream for project ID '{}' (HTTP steam method)".format(project.id))

    async def event_stream():

        try:
            with controller.notification.project_queue(project.id) as queue:
                while True:
                    msg = await queue.get_json(5)
                    yield ("{}\n".format(msg)).encode("utf-8")
        finally:
            log.info("Client has disconnected from notification for project ID '{}' (HTTP stream method)".format(project.id))
            if project.auto_close:
                # To avoid trouble with client connecting disconnecting we sleep few seconds before checking
                # if someone else is not connected
                await asyncio.sleep(5)
                if not controller.notification.project_has_listeners(project.id):
                    log.info("Project '{}' is automatically closing due to no client listening".format(project.id))
                    await project.close()

    return StreamingResponse(event_stream(), media_type="application/json")


@router.websocket("/{project_id}/notifications/ws")
async def notification_ws(project_id: UUID, websocket: WebSocket):
    """
    Receive project notifications about the controller from WebSocket.
    """

    controller = Controller.instance()
    project = controller.get_project(str(project_id))
    await websocket.accept()

    log.info("New client has connected to the notification stream for project ID '{}' (WebSocket method)".format(project.id))
    try:
        with controller.notification.project_queue(project.id) as queue:
            while True:
                notification = await queue.get_json(5)
                await websocket.send_text(notification)
    except (ConnectionClosed, WebSocketDisconnect):
        log.info("Client has disconnected from notification stream for project ID '{}' (WebSocket method)".format(project.id))
    except WebSocketException as e:
        log.warning("Error while sending to project event to WebSocket client: '{}'".format(e))
    finally:
        await websocket.close()
        if project.auto_close:
            # To avoid trouble with client connecting disconnecting we sleep few seconds before checking
            # if someone else is not connected
            await asyncio.sleep(5)
            if not controller.notification.project_has_listeners(project.id):
                log.info("Project '{}' is automatically closing due to no client listening".format(project.id))
                await project.close()


@router.get("/{project_id}/export",
            responses=responses)
async def export_project(project: Project = Depends(dep_project),
                         include_snapshots: bool = False,
                         include_images: bool = False,
                         reset_mac_addresses: bool = False,
                         compression: str = "zip"):
    """
    Export a project as a portable archive.
    """

    compression_query = compression.lower()
    if compression_query == "zip":
        compression = zipfile.ZIP_DEFLATED
    elif compression_query == "none":
        compression = zipfile.ZIP_STORED
    elif compression_query == "bzip2":
        compression = zipfile.ZIP_BZIP2
    elif compression_query == "lzma":
        compression = zipfile.ZIP_LZMA

    try:
        begin = time.time()
        # use the parent directory as a temporary working dir
        working_dir = os.path.abspath(os.path.join(project.path, os.pardir))

        async def streamer():
            with tempfile.TemporaryDirectory(dir=working_dir) as tmpdir:
                with aiozipstream.ZipFile(compression=compression) as zstream:
                    await export_controller_project(zstream,
                                                    project,
                                                    tmpdir,
                                                    include_snapshots=include_snapshots,
                                                    include_images=include_images,
                                                    reset_mac_addresses=reset_mac_addresses)
                    async for chunk in zstream:
                        yield chunk

            log.info("Project '{}' exported in {:.4f} seconds".format(project.name, time.time() - begin))

    # Will be raise if you have no space left or permission issue on your temporary directory
    # RuntimeError: something was wrong during the zip process
    except (ValueError, OSError, RuntimeError) as e:
        raise ConnectionError("Cannot export project: {}".format(e))

    headers = {"CONTENT-DISPOSITION": 'attachment; filename="{}.gns3project"'.format(project.name)}
    return StreamingResponse(streamer(), media_type="application/gns3project", headers=headers)


@router.post("/{project_id}/import",
             status_code=status.HTTP_201_CREATED,
             response_model=schemas.Project,
             responses=responses)
async def import_project(project_id: UUID, request: Request, path: Optional[Path] = None, name: Optional[str] = None):
    """
    Import a project from a portable archive.
    """

    controller = Controller.instance()
    config = Config.instance()
    if not config.get_section_config("Server").getboolean("local", False):
        raise ControllerForbiddenError("The server is not local")

    # We write the content to a temporary location and after we extract it all.
    # It could be more optimal to stream this but it is not implemented in Python.
    try:
        begin = time.time()
        # use the parent directory or projects dir as a temporary working dir
        if path:
            working_dir = os.path.abspath(os.path.join(path, os.pardir))
        else:
            working_dir = controller.projects_directory()
        with tempfile.TemporaryDirectory(dir=working_dir) as tmpdir:
            temp_project_path = os.path.join(tmpdir, "project.zip")
            async with aiofiles.open(temp_project_path, 'wb') as f:
                async for chunk in request.stream():
                    await f.write(chunk)
            with open(temp_project_path, "rb") as f:
                project = await import_controller_project(controller, str(project_id), f, location=path, name=name)

        log.info("Project '{}' imported in {:.4f} seconds".format(project.name, time.time() - begin))
    except OSError as e:
        raise ControllerError("Could not import the project: {}".format(e))
    return project.__json__()


@router.post("/{project_id}/duplicate",
             status_code=status.HTTP_201_CREATED,
             response_model=schemas.Project,
             responses={
                 **responses,
                 409: {"model": ErrorMessage, "description": "Could not duplicate project"}
             })
async def duplicate(project_data: schemas.ProjectDuplicate, project: Project = Depends(dep_project)):
    """
    Duplicate a project.
    """

    if project_data.path:
        config = Config.instance()
        if config.get_section_config("Server").getboolean("local", False) is False:
            raise ControllerForbiddenError("The server is not a local server")
        location = project_data.path
    else:
        location = None

    reset_mac_addresses = project_data.reset_mac_addresses
    new_project = await project.duplicate(name=project_data.name, location=location, reset_mac_addresses=reset_mac_addresses)
    return new_project.__json__()


@router.get("/{project_id}/files/{file_path:path}")
async def get_file(file_path: str, project: Project = Depends(dep_project)):
    """
    Return a file from a project.
    """

    path = os.path.normpath(file_path).strip('/')

    # Raise error if user try to escape
    if path[0] == ".":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    path = os.path.join(project.path, path)
    if not os.path.exists(path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return FileResponse(path, media_type="application/octet-stream")


@router.post("/{project_id}/files/{file_path:path}",
             status_code=status.HTTP_204_NO_CONTENT)
async def write_file(file_path: str, request: Request, project: Project = Depends(dep_project)):
    """
    Write a file from a project.
    """

    path = os.path.normpath(file_path).strip("/")

    # Raise error if user try to escape
    if path[0] == ".":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    path = os.path.join(project.path, path)

    try:
        async with aiofiles.open(path, 'wb+') as f:
            async for chunk in request.stream():
                await f.write(chunk)
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    except OSError as e:
        raise ControllerError(str(e))
