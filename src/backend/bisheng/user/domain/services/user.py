from base64 import b64decode
from datetime import datetime
import hashlib
import hmac
import json
import os
import time
from typing import List, TYPE_CHECKING
from urllib.parse import unquote, urlsplit

import httpx
import rsa
from fastapi import Request, Depends, UploadFile, HTTPException

from bisheng.common.constants.enums.telemetry import BaseTelemetryTypeEnum
from bisheng.common.errcode.user import (UserNameAlreadyExistError,
                                         UserNeedGroupAndRoleError, UserForbiddenError, CaptchaError, UserValidateError,
                                         UserPasswordMaxTryError, UserPasswordExpireError, UserNameTooLongError)
from bisheng.common.schemas.api import resp_200
from bisheng.common.schemas.telemetry.event_data_schema import UserLoginEventData
from bisheng.common.services import telemetry_service
from bisheng.common.services.config_service import settings
from bisheng.core.cache.redis_manager import get_redis_client_sync, get_redis_client
from bisheng.core.logger import trace_id_var
from bisheng.core.storage.minio.minio_manager import get_minio_storage, get_minio_storage_sync
from bisheng.database.models.user_group import UserGroupDao
from bisheng.user.domain.models.user import User, UserDao, UserLogin, UserRead, UserCreate
from bisheng.utils import md5_hash, get_request_ip, generate_uuid
from bisheng.utils.constants import RSA_KEY
from .auth import LoginUser, AuthJwt
from .captcha import verify_captcha
from ..const import USER_PASSWORD_ERROR, USER_CURRENT_SESSION

if TYPE_CHECKING:
    from bisheng.api.v1.schemas import CreateUserReq

# Allowed avatar file types and their MIME types
ALLOWED_AVATAR_TYPES = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/webp': '.webp',
    'image/gif': '.gif',
}
MAX_AVATAR_SIZE = 10 * 1024 * 1024  # 10MB
AVATAR_OBJECT_PREFIX = 'avatar/'


class UserService:
    @classmethod
    def _normalize_avatar_object_name(cls, avatar: str | None, bucket: str | None = None) -> str | None:
        if not avatar:
            return avatar

        avatar = avatar.strip()
        path = urlsplit(avatar).path if '://' in avatar or avatar.startswith('/') else avatar.split('?', 1)[0]
        path = unquote(path).lstrip('/')

        if bucket and path.startswith(f'{bucket}/'):
            path = path[len(bucket) + 1:]

        if path.startswith(AVATAR_OBJECT_PREFIX):
            return path
        return None

    @classmethod
    def get_avatar_share_link_sync(cls, avatar: str | None) -> str | None:
        if not avatar:
            return avatar

        minio_client = get_minio_storage_sync()
        object_name = cls._normalize_avatar_object_name(avatar, minio_client.bucket)
        if not object_name:
            return avatar
        return minio_client.get_share_link_sync(object_name)

    @classmethod
    async def get_avatar_share_link(cls, avatar: str | None) -> str | None:
        if not avatar:
            return avatar

        minio_client = await get_minio_storage()
        object_name = cls._normalize_avatar_object_name(avatar, minio_client.bucket)
        if not object_name:
            return avatar
        return await minio_client.get_share_link(object_name)

    @classmethod
    async def build_user_read(cls, user: User, **kwargs) -> UserRead:
        user_data = user.model_dump()
        user_data.update(kwargs)
        user_data['avatar'] = await cls.get_avatar_share_link(user_data.get('avatar'))
        return UserRead(**user_data)

    @classmethod
    def build_user_read_sync(cls, user: User, **kwargs) -> UserRead:
        user_data = user.model_dump()
        user_data.update(kwargs)
        user_data['avatar'] = cls.get_avatar_share_link_sync(user_data.get('avatar'))
        return UserRead(**user_data)

    @classmethod
    def decrypt_md5_password(cls, password: str):
        if value := get_redis_client_sync().get(RSA_KEY):
            private_key = value[1]
            password = md5_hash(rsa.decrypt(b64decode(password), private_key).decode('utf-8'))
        else:
            password = md5_hash(password)
        return password

    @classmethod
    def create_user(cls, request: Request, login_user: LoginUser, req_data: 'CreateUserReq'):
        """
        Create User
        """
        exists_user = UserDao.get_user_by_username(req_data.user_name)
        if exists_user:
            # Throwing an exception?
            raise UserNameAlreadyExistError.http_exception()
        user = User(
            user_name=req_data.user_name,
            password=cls.decrypt_md5_password(req_data.password),
        )
        group_ids = []
        role_ids = []
        for one in req_data.group_roles:
            group_ids.append(one.group_id)
            role_ids.extend(one.role_ids)
        if not group_ids or not role_ids:
            raise UserNeedGroupAndRoleError.http_exception()
        user = UserDao.add_user_with_groups_and_roles(user, group_ids, role_ids)
        return user

    @staticmethod
    def get_error_password_key(username: str):
        return USER_PASSWORD_ERROR.format(username)

    @classmethod
    async def clear_error_password_key(cls, username: str):
        # Count of cleanup password errors
        error_key = cls.get_error_password_key(username)
        (await get_redis_client()).delete(error_key)

    @classmethod
    async def judge_user_password(cls, db_user: User, password: str) -> None:
        redis_client = await get_redis_client()

        password_conf = await settings.get_password_conf()
        if not db_user.password:
            raise UserValidateError()

        if db_user.password == password:
            # Determine if the password has not been changed for a long time
            if password_conf.password_valid_period and password_conf.password_valid_period > 0:
                if (datetime.now() - db_user.password_update_time).days >= password_conf.password_valid_period:
                    raise UserPasswordExpireError()
            return

        # Determine if the number of errors needs to be logged
        if not password_conf.login_error_time_window or not password_conf.max_error_times:
            raise UserValidateError()
        # Number of errors plus1
        error_key = cls.get_error_password_key(db_user.user_name)
        error_num = await redis_client.aincr(error_key)
        if error_num == 1:
            # First time setupkeyExpiration date
            await redis_client.aexpire_key(error_key, password_conf.login_error_time_window * 60)
        if error_num and int(error_num) >= password_conf.max_error_times:
            # Maximum number of errors reached, account banned
            db_user.delete = 1
            await UserDao.aupdate_user(db_user)
            raise UserPasswordMaxTryError()
        raise UserValidateError()

    @classmethod
    async def user_register(cls, user: UserCreate):
        # Captcha Verification
        if settings.get_from_db('use_captcha'):
            if not user.captcha_key or not await verify_captcha(user.captcha, user.captcha_key):
                raise CaptchaError()

        db_user = User.model_validate(user)

        # check if user already exist
        user_exists = await UserDao.aget_user_by_username(db_user.user_name)
        if user_exists:
            raise UserNameAlreadyExistError()
        if len(db_user.user_name) > 30:
            raise UserNameTooLongError()
        db_user.password = cls.decrypt_md5_password(user.password)
        # Under JudgmentadminDoes the user exist
        admin = await UserDao.aget_user(1)
        if admin:
            db_user = await UserDao.add_user_and_default_role(db_user)
        else:
            db_user.user_id = 1
            db_user = await UserDao.add_user_and_admin_role(db_user)
        # Write users to the default user group
        await UserGroupDao.add_default_user_group(db_user.user_id)
        return db_user

    @classmethod
    async def user_login(cls, request: Request, user: UserLogin, auth_jwt: AuthJwt = Depends()):
        from bisheng.api.services.audit_log import AuditLogService

        if await settings.aget_from_db('use_captcha'):
            if not user.captcha_key or not await verify_captcha(user.captcha, user.captcha_key):
                raise CaptchaError()

        # get user info
        db_user = await UserDao.aget_user_by_username(user.user_name)
        # verify user exists
        if not db_user:
            return UserValidateError.return_resp()
        if db_user.delete == 1:
            raise UserForbiddenError()

        # verify password
        password = cls.decrypt_md5_password(user.password)
        await cls.judge_user_password(db_user, password)

        # gen jwt token
        access_token = LoginUser.create_access_token(user=db_user, auth_jwt=auth_jwt)

        # set cookies
        LoginUser.set_access_cookies(access_token, auth_jwt=auth_jwt)

        # Set the logged in user's currentcookie, .jwtValid for an additional hour
        redis_client = await get_redis_client()
        await redis_client.aset(USER_CURRENT_SESSION.format(db_user.user_id), access_token,
                                auth_jwt.cookie_conf.jwt_token_expire_time + 3600)

        # Log Audit Logs
        login_user = await LoginUser.init_login_user(db_user.user_id, db_user.user_name)
        AuditLogService.user_login(login_user, get_request_ip(request))

        # RecordTelemetryJournal
        await telemetry_service.log_event(user_id=db_user.user_id, event_type=BaseTelemetryTypeEnum.USER_LOGIN,
                                          trace_id=trace_id_var.get(),
                                          event_data=UserLoginEventData(method="password"))

        return resp_200(await cls.build_user_read(db_user, access_token=access_token))

    @classmethod
    async def validate_external_jwt_token(cls, token: str) -> dict:
        if cls._is_external_jwt_mock_enabled():
            return cls._get_external_jwt_mock_user(token)

        validate_url = os.getenv('BISHENG_EXTERNAL_JWT_VALIDATE_URL') or os.getenv('EXTERNAL_JWT_VALIDATE_URL')
        if not validate_url:
            raise HTTPException(status_code=503, detail='External JWT login is not configured')

        app_secret = os.getenv('BISHENG_EXTERNAL_JWT_APP_SECRET') or os.getenv('JWT_SHARED_SECRET')
        if not app_secret:
            raise HTTPException(status_code=503, detail='External JWT AppSecret is not configured')

        body = {'token': token}
        body_json = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
        timestamp = str(int(time.time() * 1000))
        sign = hmac.new(
            app_secret.encode('utf-8'),
            f'{body_json}{timestamp}'.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            'Content-Type': 'application/json',
            'X-Bisheng-AppId': os.getenv('BISHENG_EXTERNAL_JWT_APP_ID', 'bisheng_platform'),
            'X-Bisheng-Sign': sign,
            'X-Timestamp': timestamp,
        }

        timeout = float(os.getenv('BISHENG_EXTERNAL_JWT_VALIDATE_TIMEOUT', '3'))
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(validate_url, content=body_json.encode('utf-8'), headers=headers)
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail='External token validation service unavailable') from exc

        if response.status_code != 200:
            status_code = response.status_code if response.status_code in (401, 403) else 401
            raise HTTPException(status_code=status_code, detail='External token validation failed')

        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail='Invalid external token validation response') from exc

        if isinstance(payload, dict):
            status_code = payload.get('status_code', payload.get('code'))
            success = payload.get('success')
            if status_code not in (None, 0, 200) or success is False:
                raise HTTPException(
                    status_code=401,
                    detail=payload.get('status_message') or payload.get('message') or 'External token validation failed',
                )

        user_info = payload
        if isinstance(payload, dict):
            for key in ('data', 'result', 'user', 'userInfo', 'user_info'):
                if isinstance(payload.get(key), dict):
                    user_info = payload[key]
                    break
        if not isinstance(user_info, dict):
            raise HTTPException(status_code=502, detail='External token validation response missing user info')
        return user_info

    @staticmethod
    def _is_external_jwt_mock_enabled() -> bool:
        value = os.getenv('BISHENG_EXTERNAL_JWT_MOCK_ENABLED', '').strip().lower()
        return value in ('1', 'true', 'yes', 'on')

    @staticmethod
    def _get_external_jwt_mock_user(token: str) -> dict:
        mock_user_json = os.getenv('BISHENG_EXTERNAL_JWT_MOCK_USER_JSON')
        if mock_user_json:
            try:
                user_info = json.loads(mock_user_json)
            except ValueError as exc:
                raise HTTPException(status_code=503, detail='Invalid external JWT mock user JSON') from exc
            if not isinstance(user_info, dict):
                raise HTTPException(status_code=503, detail='External JWT mock user must be a JSON object')
            return user_info

        username = os.getenv('BISHENG_EXTERNAL_JWT_MOCK_USERNAME', 'external_mock_user')
        return {
            'userId': os.getenv('BISHENG_EXTERNAL_JWT_MOCK_USER_ID', 'mock_user_001'),
            'username': username,
            'realname': os.getenv('BISHENG_EXTERNAL_JWT_MOCK_REALNAME', username),
            'email': os.getenv('BISHENG_EXTERNAL_JWT_MOCK_EMAIL', f'{username}@example.com'),
            'avatar': os.getenv('BISHENG_EXTERNAL_JWT_MOCK_AVATAR', ''),
            'token': token,
        }

    @staticmethod
    def _get_external_user_name(user_info: dict) -> str:
        for key in ('username', 'user_name', 'loginName', 'login_name', 'account', 'name', 'realname'):
            value = user_info.get(key)
            if value:
                return str(value).strip()
        raise HTTPException(status_code=401, detail='External token user info missing username')

    @classmethod
    async def user_login_with_external_jwt(cls, request: Request, token: str, auth_jwt: AuthJwt = Depends()):
        from bisheng.api.services.audit_log import AuditLogService

        user_info = await cls.validate_external_jwt_token(token)
        account_name = cls._get_external_user_name(user_info)
        if len(account_name) > 30:
            raise UserNameTooLongError()

        db_user = await UserDao.aget_user_by_username(account_name)
        if not db_user:
            new_user = User(
                user_name=account_name,
                password='',
                email=user_info.get('email'),
                phone_number=user_info.get('phone_number') or user_info.get('phone'),
                avatar=user_info.get('avatar'),
                remark='external_jwt',
            )
            user_all = UserDao.get_all_users(page=1, limit=1)
            default_admin = settings.get_system_login_method().admin_username
            if len(user_all) == 0 or (default_admin and default_admin == account_name):
                db_user = await UserDao.add_user_and_admin_role(new_user)
            else:
                db_user = await UserDao.add_user_and_default_role(new_user)
            await UserGroupDao.add_default_user_group(db_user.user_id)

        if db_user.delete == 1:
            raise UserForbiddenError()

        access_token = LoginUser.create_access_token(user=db_user, auth_jwt=auth_jwt)
        LoginUser.set_access_cookies(access_token, auth_jwt=auth_jwt)

        redis_client = await get_redis_client()
        await redis_client.aset(USER_CURRENT_SESSION.format(db_user.user_id), access_token,
                                auth_jwt.cookie_conf.jwt_token_expire_time + 3600)

        login_user = await LoginUser.init_login_user(db_user.user_id, db_user.user_name)
        AuditLogService.user_login(login_user, get_request_ip(request))
        await telemetry_service.log_event(user_id=db_user.user_id, event_type=BaseTelemetryTypeEnum.USER_LOGIN,
                                          trace_id=trace_id_var.get(),
                                          event_data=UserLoginEventData(method="external_jwt"))

        return resp_200(await cls.build_user_read(db_user, access_token=access_token))

    @classmethod
    async def user_login_with_external_jwt_data(cls, request: Request, token: str, auth_jwt: AuthJwt = Depends()) -> UserRead:
        response = await cls.user_login_with_external_jwt(request, token=token, auth_jwt=auth_jwt)
        return response.data

    @classmethod
    def get_user_all_info(cls, *, start_time: datetime = None, end_time: datetime = None, user_ids: List[int] = None,
                          page: int = 1, page_size: int = 100) -> List[User]:
        """ Get user information, including user group and role information """
        return UserDao.get_user_with_group_role(page=page, page_size=page_size, user_ids=user_ids,
                                                start_time=start_time, end_time=end_time)

    @classmethod
    def get_first_user(cls) -> User | None:
        """ Get the first user """
        return UserDao.get_first_user()

    @classmethod
    async def get_user_by_id(cls, user_id: int) -> User | None:
        """ Get user by username """
        return await UserDao.aget_user(user_id)

    @classmethod
    async def update_avatar(cls, user_id: int, file: UploadFile) -> str:
        """
        Update user avatar
        :param user_id: User ID
        :param file: Uploaded avatar file
        :return: Avatar URL
        """
        # Validate file type
        if file.content_type not in ALLOWED_AVATAR_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f'Invalid file type. Allowed types: jpg, png, webp, gif'
            )

        # Read file content to check size
        content = await file.read()
        if len(content) > MAX_AVATAR_SIZE:
            raise HTTPException(
                status_code=400,
                detail='File size exceeds limit. Maximum size: 10MB'
            )

        # Generate object name for MinIO
        file_ext = ALLOWED_AVATAR_TYPES[file.content_type]
        object_name = f'avatar/{user_id}/{generate_uuid()}{file_ext}'

        # Upload to MinIO
        minio_client = await get_minio_storage()
        await minio_client.put_object(
            object_name=object_name,
            file=content,
            content_type=file.content_type,
        )

        # Update user avatar in database
        user = await UserDao.aget_user(user_id)
        if user:
            user.avatar = object_name
            await UserDao.aupdate_user(user)

        return await cls.get_avatar_share_link(object_name)
