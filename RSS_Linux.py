#!/usr/bin/env python3
"""
PoC RSS Loader for OpenCTI - Linux Version
Автоматически загружает Proof-of-Concept файлы из GitHub репозиториев
через RSS-ленту и создает объекты в OpenCTI
"""

import os
import requests
import feedparser
import json
import hashlib
import logging
import sys
import re
import time
import yaml
import shutil
import uuid
import subprocess
from datetime import datetime, timedelta
from urllib.parse import urlparse
from pathlib import Path
from pycti import OpenCTIApiClient
from logging.handlers import TimedRotatingFileHandler

# Загрузка конфигурации
def load_config():
    """Загружает конфигурацию из YAML файла"""
    try:
        with open('config.yml', 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print("[ERROR] Config file 'config.yml' not found!")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"[ERROR] Error parsing config file: {e}")
        sys.exit(1)

# Загружаем конфигурацию
config = load_config()

# Настройка логирования на 24 часа с последующей перезаписью файла
def setup_logging():
    """Логирование: вести 24 часа, затем начинать файл заново без хранения бэкапов"""
    log_config = config['logging']

    formatter = logging.Formatter(log_config['format'])

    # Ротация раз в 24 часа; не храним бэкапы
    file_handler = TimedRotatingFileHandler(
        filename=log_config['file'],
        when='H',
        interval=log_config.get('rotation_hours', 24),
        backupCount=0,
        encoding='utf-8'
    )
    # Кастомный ротатор: удаляет старый файл (source), бэкап не создается
    def _delete_rotator(source, dest):
        try:
            if os.path.exists(source):
                os.remove(source)
        except Exception:
            pass
    file_handler.rotator = _delete_rotator

    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(getattr(logging, log_config['level']))
    # Сбрасываем старые хендлеры
    logger.handlers = []
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

# Инициализируем логирование
logger = setup_logging()

# Извлекаем настройки из конфигурации
OPENCTI_URL = config['opencti']['url']
OPENCTI_TOKEN = config['opencti']['token']
POC_RSS_URL = config['rss']['url']
CACHE_FILE = None  # cache disabled
CHECK_INTERVAL = config['monitoring']['check_interval']
MAX_RETRIES = config['monitoring']['max_retries']
RETRY_DELAY = config['monitoring']['retry_delay']
WORK_DIR = os.path.join(os.getcwd(), config['files']['work_dir'])
MAX_FILE_SIZE = config['files']['max_file_size']
MIN_FILE_SIZE = config['files']['min_file_size']
# Количество элементов, которые обрабатываются при старте до перехода в realtime
BOOTSTRAP_COUNT = config.get('monitoring', {}).get('bootstrap_count', 20)

# Настройки NVD API
NVD_API_MAX_RETRIES = config.get('nvd_api', {}).get('max_retries', 3)
NVD_API_BASE_DELAY = config.get('nvd_api', {}).get('base_delay', 2)
NVD_API_REQUEST_DELAY = config.get('nvd_api', {}).get('request_delay', 5)  # секунды между запросами

# Настройки исключений берём только из config.yml
EXCLUDED_EXTENSIONS = set(ext.lower() for ext in config.get('excluded_extensions', []))
EXCLUDED_FILE_NAMES = set(name.lower() for name in config.get('excluded_file_name_patterns', []))
EXCLUDED_DIR_NAMES = set(name.lower() for name in config.get('excluded_dir_names', []))
 
# Создаем рабочую директорию
os.makedirs(WORK_DIR, exist_ok=True)

# Инициализация клиентов
def init_clients():
    """Безопасная инициализация клиентов"""
    try:
        # OpenCTI клиент с SSL настройками
        opencti_api = OpenCTIApiClient(
            OPENCTI_URL, 
            OPENCTI_TOKEN, 
            ssl_verify=config['opencti']['ssl_verify']
        )
        logger.info("[OK] OpenCTI client initialized")
        return opencti_api
            
    except Exception as e:
        logger.error(f"[ERROR] Failed to initialize OpenCTI client: {e}")
        sys.exit(1)

# Инициализируем клиента
opencti_api = init_clients()

def load_cache():
    """Загружает кеш из файла"""
    cache_file = 'poc_cache.json'
    try:
        if os.path.exists(cache_file):
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
                
                # Подсчитываем статистику по типам записей
                total_items = len(cache_data)
                processed_items = 0
                empty_repos = 0
                empty_repos_with_tools = 0
                filtered_repos = 0
                
                for item in cache_data.values():
                    status = item.get('status', 'processed')
                    if status == 'no_suitable_files':
                        empty_repos += 1
                        if item.get('tool_created', False):
                            empty_repos_with_tools += 1
                    elif status == 'all_files_filtered':
                        filtered_repos += 1
                    else:
                        processed_items += 1
                
                logger.info(f"[CACHE] Loaded cache with {total_items} items:")
                logger.info(f"[CACHE] - Successfully processed: {processed_items}")
                logger.info(f"[CACHE] - Empty repositories: {empty_repos} ({empty_repos_with_tools} with Tool+Vulnerability)")
                logger.info(f"[CACHE] - All files filtered: {filtered_repos}")
                
                return cache_data
        else:
            logger.info("[CACHE] No cache file found, starting with empty cache")
            return {}
    except Exception as e:
        logger.warning(f"[CACHE] Error loading cache: {e}, starting with empty cache")
        return {}

def save_cache(cache):
    """Сохраняет кеш в файл"""
    cache_file = 'poc_cache.json'
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        
        # Подсчитываем статистику по типам записей
        total_items = len(cache)
        processed_items = 0
        empty_repos = 0
        empty_repos_with_tools = 0
        filtered_repos = 0
        
        for item in cache.values():
            status = item.get('status', 'processed')
            if status == 'no_suitable_files':
                empty_repos += 1
                if item.get('tool_created', False):
                    empty_repos_with_tools += 1
            elif status == 'all_files_filtered':
                filtered_repos += 1
            else:
                processed_items += 1
        
        logger.info(f"[CACHE] Saved cache with {total_items} items:")
        logger.info(f"[CACHE] - Successfully processed: {processed_items}")
        logger.info(f"[CACHE] - Empty repositories: {empty_repos} ({empty_repos_with_tools} with Tool+Vulnerability)")
        logger.info(f"[CACHE] - All files filtered: {filtered_repos}")
        
        return True
    except Exception as e:
        logger.error(f"[CACHE] Error saving cache: {e}")
        return False

def extract_cve_id(title):
    """Извлекает CVE ID из заголовка"""
    cve_pattern = r'CVE-\d{4}-\d{4,7}'
    match = re.search(cve_pattern, title, re.IGNORECASE)
    return match.group(0) if match else None

def check_git_installed():
    """Проверяет, установлен ли git в системе"""
    try:
        result = subprocess.run(['git', '--version'], 
                              capture_output=True, text=True, check=True)
        logger.info(f"[GIT] Git version: {result.stdout.strip()}")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.error("[ERROR] Git is not installed or not available in PATH")
        return False

def clone_repository(repository_url, cve_id):
    """Клонирует GitHub репозиторий используя git clone"""
    try:
        # Извлекаем owner и repo из URL
        parts = repository_url.replace("https://github.com/", "").split("/")
        if len(parts) < 2:
            logger.error(f"Invalid GitHub URL: {repository_url}")
            return None
        
        owner, repo = parts[0], parts[1]
        
        # Создаем папку для CVE
        poc_dir = os.path.join(WORK_DIR, cve_id)
        
        # Удаляем существующую папку если она есть
        if os.path.exists(poc_dir):
            shutil.rmtree(poc_dir)
            logger.info(f"[CLEANUP] Removed existing directory: {poc_dir}")
        
        os.makedirs(poc_dir, exist_ok=True)
        
        logger.info(f"[GIT] Cloning repository for {cve_id}: {repository_url}")
        
        # Подготавливаем команду git clone
        git_cmd = ['git', 'clone', '--depth', '1', '--single-branch']
        git_cmd.extend([repository_url, poc_dir])
        
        # Выполняем git clone
        result = subprocess.run(git_cmd, 
                              capture_output=True, 
                              text=True, 
                              cwd=WORK_DIR,
                              timeout=300)  # 5 минут таймаут
        
        if result.returncode == 0:
            logger.info(f"[OK] Repository cloned successfully: {poc_dir}")
            
            # Удаляем .git директорию для экономии места
            git_dir = os.path.join(poc_dir, '.git')
            if os.path.exists(git_dir):
                shutil.rmtree(git_dir)
                logger.info(f"[CLEANUP] Removed .git directory from {poc_dir}")
            
            return poc_dir
        else:
            logger.error(f"[ERROR] Git clone failed: {result.stderr}")
            return None
                
    except subprocess.TimeoutExpired:
        logger.error(f"[ERROR] Git clone timeout for {cve_id}")
        return None
    except Exception as e:
        logger.error(f"[ERROR] Error cloning repository for {cve_id}: {e}")
        return None

def get_repository_files(repo_path):
    """Получает список всех файлов из клонированного репозитория"""
    try:
        if not os.path.exists(repo_path):
            logger.error(f"Repository path does not exist: {repo_path}")
            return []
        
        file_list = []
        
        # Рекурсивно обходим все файлы в репозитории
        for root, dirs, files in os.walk(repo_path):
            # Пропускаем скрытые директории
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            
            for file_name in files:
                # Получаем относительный путь от корня репозитория
                rel_path = os.path.relpath(os.path.join(root, file_name), repo_path)
                
                # Получаем полный путь к файлу
                full_path = os.path.join(root, file_name)
                
                try:
                    # Получаем размер файла
                    file_size = os.path.getsize(full_path)
                    
                    file_info = {
                        'name': file_name,
                        'path': rel_path.replace('\\', '/'),  # Нормализуем пути для Linux
                        'local_path': full_path,
                        'size': file_size,
                        'type': 'file'
                    }
                    file_list.append(file_info)
                except OSError as e:
                    logger.debug(f"Could not get file info for {full_path}: {e}")
                    continue
        
        logger.info(f"[FILES] Found {len(file_list)} files in cloned repository")
        return file_list
        
    except Exception as e:
        logger.error(f"[ERROR] Error getting files from repository: {e}")
        return []

def filter_files(files_list):
    """
    Фильтрует список файлов, исключая файлы из черного списка
    """
    if not files_list:
        return []
    
    filtered_files = []
    total_files = len(files_list)
    excluded_count = 0
    
    # Счетчики для разных причин исключения
    excluded_by_extension = 0
    excluded_by_name = 0
    excluded_by_directory = 0
    excluded_by_size = 0
    
    logger.info(f"[SEARCH] Filtering {total_files} files...")
    
    for file_info in files_list:
        exclusion_reason = is_file_excluded(file_info)
        if not exclusion_reason:
            filtered_files.append(file_info)
            logger.debug(f"[OK] Included: {file_info['path']}")
        else:
            excluded_count += 1
            # Подсчитываем причины исключения
            if "extension" in exclusion_reason:
                excluded_by_extension += 1
            elif "name pattern" in exclusion_reason:
                excluded_by_name += 1
            elif "directory name" in exclusion_reason:
                excluded_by_directory += 1
            elif "size" in exclusion_reason:
                excluded_by_size += 1
            
            logger.debug(f"[FILTER] Excluded: {file_info['path']} - {exclusion_reason}")
    
    # Выводим детальную статистику фильтрации
    if excluded_count > 0:
        logger.info(f"[STATS] Filtering results: {len(filtered_files)}/{total_files} files selected, {excluded_count} excluded")
        logger.info(f"[FILTER] Exclusion breakdown: {excluded_by_extension} by extension, {excluded_by_name} by name, {excluded_by_directory} by directory, {excluded_by_size} by size")
    else:
        logger.info(f"[STATS] All {total_files} files passed filtering")
    
    return filtered_files

def is_file_excluded(file_info):
    """
    Проверяет, нужно ли исключить файл на основе:
    - расширений из черного списка (config.yml)
    - имен файлов (например, readme, license) из черного списка (config.yml)
    - имен директорий в пути (например, .git) из черного списка (config.yml)
    - минимального/максимального размера
    Возвращает строку с причиной исключения или None если файл прошел все фильтры
    """
    file_name = file_info['name']
    file_stem, file_ext = os.path.splitext(file_name)
    file_ext = file_ext.lower()

    # 1) Проверка по расширению
    if file_ext in EXCLUDED_EXTENSIONS:
        return f"excluded by extension: {file_ext}"

    # 2) Проверка по имени файла (без расширения)
    stem_normalized = file_stem.replace('-', '_').lower()
    if stem_normalized in EXCLUDED_FILE_NAMES:
        return f"excluded by name pattern: {stem_normalized}"

    # 3) Проверка директорий в относительном пути
    path_value = file_info.get('path', '')
    if path_value:
        segments = [seg.lower() for seg in path_value.split('/') if seg]
        if any(seg in EXCLUDED_DIR_NAMES for seg in segments):
            return f"excluded by directory name: {path_value}"

    # 4) Проверка по размеру файла
    file_size = file_info.get('size', 0)
    if file_size > MAX_FILE_SIZE or file_size < MIN_FILE_SIZE:
        return f"excluded by size: {file_size} bytes (min: {MIN_FILE_SIZE}, max: {MAX_FILE_SIZE})"

    return None  # Файл прошел все фильтры

def get_file_hashes(file_path):
    """Вычисляет хеши файла"""
    hashers = {
        'md5': hashlib.md5(),
        'sha1': hashlib.sha1(),
        'sha256': hashlib.sha256()
    }
    
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            for hasher in hashers.values():
                hasher.update(chunk)
    
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}

def download_repository_files(repository_url, cve_id):
    """Клонирует репозиторий и получает список файлов"""
    try:
        # Клонируем репозиторий
        repo_path = clone_repository(repository_url, cve_id)
        
        if not repo_path:
            logger.error(f"[ERROR] Failed to clone repository for {cve_id}")
            return [], None
        
        # Получаем список файлов
        files_list = get_repository_files(repo_path)
        
        if not files_list:
            logger.info(f"[INFO] No files found in repository for {cve_id} (repository might be empty)")
            return [], repo_path
        
        # Фильтруем файлы для обработки
        filtered_files = filter_files(files_list)
        
        if not filtered_files:
            logger.info(f"[INFO] No files to process for {cve_id} after filtering (this is normal for repositories with only documentation/media files)")
            return [], repo_path
        
        logger.info(f"[TARGET] Found {len(filtered_files)} files to process for {cve_id}")
        
        # Подготавливаем информацию о файлах для OpenCTI
        processed_files = []
        
        for file_info in filtered_files:
            try:
                # Вычисляем хеши
                hashes = get_file_hashes(file_info['local_path'])
                
                processed_file_info = {
                    'cve_id': cve_id,
                    'file_name': file_info['name'],
                    'file_path': file_info['path'],
                    'local_path': file_info['local_path'],
                    'hashes': hashes,
                    'size': file_info['size'],
                    'processed_at': datetime.now().isoformat()
                }
                
                processed_files.append(processed_file_info)
                logger.debug(f"[OK] Processed: {file_info['path']}")
                    
            except Exception as e:
                logger.error(f"[ERROR] Error processing {file_info.get('name', 'unknown')}: {e}")
        
        logger.info(f"[STATS] Processing summary for {cve_id}: {len(processed_files)} files processed from {repo_path}")
        return processed_files, repo_path
        
    except Exception as e:
        logger.error(f"[ERROR] Error processing repository for {cve_id}: {e}")
        return [], None

def execute_graphql(query, variables=None):
    """Выполняет GraphQL-запрос"""
    data = {"query": query}
    if variables:
        data["variables"] = variables
        
    try:
        response = requests.post(f"{OPENCTI_URL}/graphql", headers={
            "Authorization": f"Bearer {OPENCTI_TOKEN}",
            "Content-Type": "application/json"
        }, data=json.dumps(data), verify=config['opencti']['ssl_verify'])
        response.raise_for_status()
        result = response.json()
        
        if "errors" in result:
            logger.error(f"[ERROR] GraphQL Error: {json.dumps(result['errors'], indent=2)}")
            return None
            
        return result.get("data")
    except Exception as e:
        logger.error(f"[ERROR] Request failed: {str(e)}")
        return None

def _graphql_introspect_fields(type_name: str):
    try:
        query = '''
        query Introspect($type: String!) {
          __type(name: $type) {
            name
            fields { name }
          }
        }'''
        data = {"query": query, "variables": {"type": type_name}}
        resp = requests.post(
            f"{OPENCTI_URL}/graphql",
            headers={"Authorization": f"Bearer {OPENCTI_TOKEN}", "Content-Type": "application/json"},
            data=json.dumps(data),
            timeout=30,
            verify=config['opencti']['ssl_verify']
        )
        resp.raise_for_status()
        j = resp.json()
        fields = j.get('data', {}).get('__type', {}).get('fields', [])
        return [f.get('name') for f in fields]
    except Exception:
        return []

def _graphql_list_mutations():
    try:
        query = '{ __schema { mutationType { fields { name } } } }'
        resp = requests.post(
            f"{OPENCTI_URL}/graphql",
            headers={"Authorization": f"Bearer {OPENCTI_TOKEN}", "Content-Type": "application/json"},
            data=json.dumps({"query": query}),
            timeout=30,
            verify=config['opencti']['ssl_verify']
        )
        resp.raise_for_status()
        j = resp.json()
        fields = j.get('data', {}).get('__schema', {}).get('mutationType', {}).get('fields', [])
        return [f.get('name') for f in fields]
    except Exception:
        return []

def attach_file_to_object(stix_core_object_id: str, file_path: str) -> bool:
    """Прикрепляет файл к существующему объекту (например, Tool) c учетом версии OpenCTI.
    Возвращает True/False."""
    try:
        if not os.path.isfile(file_path):
            logger.error(f"[ERROR] File not found for attachment: {file_path}")
            return False

        # Discover valid upload fields for current OpenCTI version
        nested_upload_candidates = [
            "addFile", "fileAdd", "fileUpload", "upload", "importPush"
        ]
        editor_mutations = [
            ("stixCoreObjectEdit", "StixCoreObjectEditMutations"),
            ("stixDomainObjectEdit", "StixDomainObjectEditMutations"),
        ]
        graphql_candidates = []

        # Try nested editor mutations first
        for root_field, type_name in editor_mutations:
            available = _graphql_introspect_fields(type_name)
            for nested in nested_upload_candidates:
                if nested in available:
                    graphql_candidates.append({
                        "kind": "nested",
                        "root": root_field,
                        "nested": nested,
                    })
                    break  # pick first match per editor type

        # Fallback: direct mutation names at root
        root_fields = _graphql_list_mutations()
        for direct in ["stixCoreObjectAddFile", "stixDomainObjectAddFile"]:
            if direct in root_fields:
                graphql_candidates.append({
                    "kind": "direct",
                    "name": direct,
                })

        if not graphql_candidates:
            logger.warning("[WARNING] No suitable file upload mutation discovered in schema; skipping file attachment")
            return False

        file_map = json.dumps({"0": ["variables.file"]})

        with open(file_path, 'rb') as f:
            for cand in graphql_candidates:
                f.seek(0)
                if cand["kind"] == "nested":
                    query = f"""
                    mutation AttachFile($id: ID!, $file: Upload!) {{
                      {cand['root']}(id: $id) {{
                        {cand['nested']}(file: $file) {{
                          id
                        }}
                      }}
                    }}
                    """
                else:
                    query = f"""
                    mutation AttachFile($id: ID!, $file: Upload!) {{
                      {cand['name']}(id: $id, file: $file) {{ id }}
                    }}
                    """

                operations = json.dumps({
                    "query": query,
                    "variables": {"id": stix_core_object_id, "file": None},
                })
                files = {
                    "operations": (None, operations, "application/json"),
                    "map": (None, file_map, "application/json"),
                    "0": (os.path.basename(file_path), f, "application/octet-stream"),
                }
                try:
                    response = requests.post(
                        f"{OPENCTI_URL}/graphql",
                        headers={"Authorization": f"Bearer {OPENCTI_TOKEN}"},
                        files=files,
                        timeout=60,
                        verify=config['opencti']['ssl_verify']
                    )
                    response.raise_for_status()
                    result = response.json()
                    if "errors" in result:
                        logger.warning(f"[WARNING] Attach attempt failed ({cand}): {json.dumps(result['errors'])}")
                        continue
                    logger.info(f"[OK] File '{os.path.basename(file_path)}' attached to object {stix_core_object_id}")
                    return True
                except Exception as e:
                    logger.warning(f"[WARNING] Attach attempt raised exception for {cand}: {str(e)}")
                    continue

        logger.warning("[WARNING] Failed to attach file to object with discovered mutations")
        return False

    except Exception as e:
        logger.error(f"[ERROR] Failed to attach file: {str(e)}")
        return False

def get_or_create_label(label_name, label_color="#ff9800"):
    """
    Получает или создает лейбл с указанным именем.
    Возвращает ID лейбла.
    """
    # Пытаемся найти существующий лейбл
    find_query = """
    query Labels($filters: FilterGroup) {
        labels(filters: $filters) {
            edges {
                node {
                    id
                    value
                    color
                }
            }
        }
    }
    """
    
    find_variables = {
        "filters": {
                "mode": "and",
            "filters": [{"key": "value", "values": [label_name]}],
                "filterGroups": []
            }
    }
    
    result = execute_graphql(find_query, find_variables)
    
    # Если лейбл найден - возвращаем первый
    if result and "labels" in result:
        edges = result["labels"].get("edges", [])
        if edges:
            existing_label = edges[0]["node"]
            logger.info(f"[OK] Using existing label: {label_name} (ID: {existing_label['id']})")
            return existing_label["id"]
    
    # Создаем новый лейбл, если не найден
    create_query = """
    mutation LabelAdd($input: LabelAddInput!) {
        labelAdd(input: $input) {
            id
            value
            color
        }
    }
    """
    
    create_variables = {
        "input": {
            "value": label_name,
            "color": label_color
        }
    }
    
    result = execute_graphql(create_query, create_variables)
    if result and "labelAdd" in result:
        new_label = result["labelAdd"]
        logger.info(f"[OK] Created new label: {label_name} (ID: {new_label['id']})")
        return new_label["id"]
    
    logger.error(f"[ERROR] Failed to get or create label: {label_name}")
    return None

def get_cve_label_id(cve_id):
    """Получает или создает лейбл для CVE ID с уникальным цветом"""
    # Генерируем цвет на основе CVE ID для уникальности
    hash_object = hashlib.md5(cve_id.encode())
    hash_hex = hash_object.hexdigest()
    
    # Используем первые 6 символов хеша как цвет
    color = f"#{hash_hex[:6]}"
    
    return get_or_create_label(cve_id, color)

def get_author_label_id(author_name):
    """Получает или создает лейбл для автора с уникальным цветом"""
    # Генерируем цвет на основе имени автора для уникальности
    hash_object = hashlib.md5(author_name.encode())
    hash_hex = hash_object.hexdigest()
    
    # Используем первые 6 символов хеша как цвет
    color = f"#{hash_hex[:6]}"
    
    return get_or_create_label(author_name, color)

def create_identity(owner_name, cve_id=None):
    """Создает Identity (автора репозитория) с лейблами"""
    # Получаем или создаем лейблы для автора
    identity_label_ids = []
    
    # Лейбл PoC (синий цвет)
    poc_label_id = get_or_create_label("PoC", "#2196F3")
    if poc_label_id:
        identity_label_ids.append(poc_label_id)
    
    # Лейбл CVE ID (уникальный цвет) - если передан CVE ID
    if cve_id:
        cve_label_id = get_cve_label_id(cve_id)
        if cve_label_id:
            identity_label_ids.append(cve_label_id)
    
    query = """
    mutation IdentityAdd($input: IdentityAddInput!) {
        identityAdd(input: $input) {
            id
            standard_id
            name
            description
            objectLabel {
                id
                value
                color
            }
        }
    }
    """
    
    # Подготавливаем входные данные
    input_data = {
        "name": owner_name,
        "description": f"GitHub repository owner: {owner_name}",
        "type": "Individual"
    }
    
    if identity_label_ids:
        input_data["objectLabel"] = identity_label_ids
    
    variables = {"input": input_data}
    
    result = execute_graphql(query, variables)
    if not result or "identityAdd" not in result:
        return None
        
    identity = result["identityAdd"]
    logger.info(f"[OK] Created Identity: {identity['id']} for {owner_name} with {len(identity_label_ids)} labels")
    return identity

def create_external_reference(repository_url, description):
    """Создает External Reference для репозитория"""
    query = """
    mutation ExternalReferenceAdd($input: ExternalReferenceAddInput!) {
        externalReferenceAdd(input: $input) {
            id
            source_name
            url
            description
        }
    }
    """
    
    variables = {
        "input": {
            "source_name": "GitHub Repository",
            "url": repository_url,
            "description": description
        }
    }
    
    result = execute_graphql(query, variables)
    if not result or "externalReferenceAdd" not in result:
        return None
        
    ref = result["externalReferenceAdd"]
    logger.info(f"[OK] Created External Reference: {ref['id']} for {repository_url}")
    return ref

def create_tool(cve_id, repository_url, description, tool_version=None, identity_id=None, external_ref_id=None, additional_labels=None):
    """Создает объект Tool для PoC с лейблами"""
    # Извлекаем owner и repo из URL
    parts = repository_url.replace("https://github.com/", "").split("/")
    if len(parts) < 2:
        logger.error(f"[ERROR] Invalid GitHub URL: {repository_url}")
        return None
    
    owner, repo = parts[0], parts[1]
    tool_name = f"{repo}/{cve_id}"  # Изменено с owner/cve_id на repo/cve_id
    
    # Получаем или создаем лейблы
    label_ids = []
    
    # Лейбл PoC (синий цвет)
    poc_label_id = get_or_create_label("PoC", "#2196F3")
    if poc_label_id:
        label_ids.append(poc_label_id)
    
    # Лейбл Awaiting Analysis (ярко желтый цвет)
    analysis_label_id = get_or_create_label("Awaiting Analysis", "#FFEB3B")
    if analysis_label_id:
        label_ids.append(analysis_label_id)
    
    # Лейбл CVE ID (уникальный цвет)
    cve_label_id = get_cve_label_id(cve_id)
    if cve_label_id:
        label_ids.append(cve_label_id)
    
    # Лейбл автора (уникальный цвет)
    author_label_id = get_author_label_id(owner)
    if author_label_id:
        label_ids.append(author_label_id)
    
    # Добавляем дополнительные лейблы, если переданы
    if additional_labels:
        for label_id in additional_labels:
            if label_id and label_id not in label_ids:
                label_ids.append(label_id)
    
    query = """
    mutation ToolAdd($input: ToolAddInput!) {
        toolAdd(input: $input) {
            id
            name
            description
            tool_version
            tool_types
            createdBy {
                id
                name
            }
            externalReferences {
                edges {
                    node {
                        id
                        source_name
                        url
                    }
                }
            }
            objectLabel {
                id
                value
                color
            }
        }
    }
    """
    
    # Подготавливаем входные данные
    input_data = {
        "name": tool_name,
        "description": description,
        "tool_types": ["Proof of Concept", "Exploit"]
    }
    
    if tool_version:
        input_data["tool_version"] = tool_version
    
    if identity_id:
        input_data["createdBy"] = identity_id
    
    if external_ref_id:
        input_data["externalReferences"] = [external_ref_id]
    
    if label_ids:
        input_data["objectLabel"] = label_ids
    
    variables = {"input": input_data}
    
    result = execute_graphql(query, variables)
    if not result or "toolAdd" not in result:
        return None
        
    tool = result["toolAdd"]
    logger.info(f"[OK] Created Tool: {tool['id']} for {cve_id} with {len(label_ids)} labels")
    return tool

def extract_tool_version(repository_url, cve_id):
    """Извлекает версию инструмента из репозитория локально (без GitHub API)"""
    try:
        # Извлекаем owner и repo из URL
        parts = repository_url.replace("https://github.com/", "").split("/")
        if len(parts) < 2:
            return None
        
        owner, repo = parts[0], parts[1]
        
        # Проверяем файлы, которые могут содержать версию
        version_files = [
            "package.json",
            "setup.py", 
            "pyproject.toml",
            "Cargo.toml",
            "go.mod",
            "requirements.txt",
            "VERSION",
            "version.txt"
        ]
        
        # Ищем файлы версии в уже клонированном репозитории
        # Эта функция будет вызываться после клонирования, поэтому
        # мы можем искать файлы локально
        for file_name in version_files:
            try:
                # Ищем файл в рабочей директории
                file_path = os.path.join(WORK_DIR, cve_id, file_name)
                if os.path.isfile(file_path):
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content_text = f.read()
                        
                        # Ищем версию в разных форматах
                        import re
                        
                        # package.json
                        if file_name == "package.json":
                            version_match = re.search(r'"version"\s*:\s*"([^"]+)"', content_text)
                            if version_match:
                                return version_match.group(1)
                        
                        # setup.py
                        elif file_name == "setup.py":
                            version_match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content_text)
                            if version_match:
                                return version_match.group(1)
                        
                        # pyproject.toml
                        elif file_name == "pyproject.toml":
                            version_match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content_text)
                            if version_match:
                                return version_match.group(1)
                        
                        # Cargo.toml
                        elif file_name == "Cargo.toml":
                            version_match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content_text)
                            if version_match:
                                return version_match.group(1)
                        
                        # go.mod
                        elif file_name == "go.mod":
                            version_match = re.search(r'v(\d+\.\d+\.\d+)', content_text)
                            if version_match:
                                return version_match.group(1)
                        
                        # Простые файлы версии
                        elif file_name in ["VERSION", "version.txt"]:
                            version_match = re.search(r'(\d+\.\d+\.\d+)', content_text)
                            if version_match:
                                return version_match.group(1)
            except Exception as e:
                logger.debug(f"[DEBUG] Error checking {file_name}: {e}")
                continue
        
        return None

    except Exception as e:
        logger.debug(f"[DEBUG] Error extracting version: {e}")
        return None

def get_cve_data_from_nvd(cve_id, max_retries=3, base_delay=2):
    """Получает данные CVE из NVD API с обработкой rate limiting и повторными попытками"""
    nvd_url = f"https://services.nvd.nist.gov/rest/json/cves/2.0?cveId={cve_id}"
    
    for attempt in range(max_retries):
        try:
            logger.info(f"[NVD] Querying NVD API for {cve_id} (attempt {attempt + 1}/{max_retries})")
            response = requests.get(nvd_url, timeout=10)
            
            # Обрабатываем rate limiting
            if response.status_code == 429:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)  # Экспоненциальная задержка
                    logger.warning(f"[NVD] Rate limit exceeded for {cve_id}, waiting {delay} seconds before retry...")
                    time.sleep(delay)
                    continue
                else:
                    logger.error(f"[ERROR] Rate limit exceeded for {cve_id} after {max_retries} attempts")
                    return None
            
            response.raise_for_status()
            data = response.json()
            
            if "vulnerabilities" not in data or len(data["vulnerabilities"]) == 0:
                logger.error(f"[ERROR] No data found for {cve_id} in NVD")
                return None
                
            cve_item = data["vulnerabilities"][0]["cve"]
            
            # Извлекаем английское описание
            descriptions = [desc["value"] for desc in cve_item["descriptions"] if desc["lang"] == "en"]
            description = descriptions[0] if descriptions else "No description available"
            
            # Извлекаем метрики CVSSv3.1
            cvss_metrics = cve_item.get("metrics", {}).get("cvssMetricV31", [])
            if not cvss_metrics:
                cvss_metrics = cve_item.get("metrics", {}).get("cvssMetricV30", [])
            
            cvss_data = {}
            if cvss_metrics:
                cvss_data = cvss_metrics[0]["cvssData"]
            
            # Извлекаем и форматируем даты
            published = cve_item.get("published", "")
            modified = cve_item.get("lastModified", "")
            
            # Добавляем временную зону (UTC)
            if published: published += "Z"
            if modified: modified += "Z"
            
            # Формируем результат
            result = {
                "name": cve_id,
                "description": description,
                "base_score": cvss_data.get("baseScore", 0.0),
                "base_severity": cvss_data.get("baseSeverity", "MEDIUM"),
                "attack_vector": cvss_data.get("attackVector", "NETWORK"),
                "confidentiality_impact": cvss_data.get("confidentialityImpact", "HIGH"),
                "integrity_impact": cvss_data.get("integrityImpact", "HIGH"),
                "availability_impact": cvss_data.get("availabilityImpact", "HIGH"),
                "published": published,
                "modified": modified
            }
            
            # Приводим значения к верхнему регистру для соответствия OpenCTI
            for key in ["base_severity", "attack_vector", "confidentiality_impact", 
                        "integrity_impact", "availability_impact"]:
                if key in result and isinstance(result[key], str):
                    result[key] = result[key].upper()
            
            logger.info(f"[OK] Retrieved CVE data for {cve_id} on attempt {attempt + 1}")
            return result
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"[NVD] Rate limit exceeded for {cve_id}, waiting {delay} seconds before retry...")
                    time.sleep(delay)
                    continue
                else:
                    logger.error(f"[ERROR] Rate limit exceeded for {cve_id} after {max_retries} attempts")
                    return None
            else:
                logger.error(f"[ERROR] HTTP error for {cve_id}: {e}")
                return None
        except Exception as e:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"[NVD] Error for {cve_id} (attempt {attempt + 1}): {e}, retrying in {delay} seconds...")
                time.sleep(delay)
                continue
            else:
                logger.error(f"[ERROR] Failed to get CVE data for {cve_id} after {max_retries} attempts: {str(e)}")
                return None
    
    return None

def create_cve_external_reference(cve_id):
    """Создает External Reference для CVE"""
    query = """
    mutation ExternalReferenceAdd($input: ExternalReferenceAddInput!) {
        externalReferenceAdd(input: $input) {
            id
            source_name
            url
            description
        }
    }
    """
    
    variables = {
        "input": {
            "source_name": "CVE Reference",
            "url": f"https://nvd.nist.gov/vuln/detail/{cve_id}",
            "description": f"Official NVD reference for {cve_id}"
        }
    }
    
    result = execute_graphql(query, variables)
    if not result or "externalReferenceAdd" not in result:
        return None
        
    ref = result["externalReferenceAdd"]
    logger.info(f"[OK] Created CVE External Reference: {ref['id']} for {cve_id}")
    return ref

def create_vulnerability_from_nvd(cve_data, external_ref_id, labels=None):
    """Создает объект Vulnerability с полными данными из NVD без привязки автора"""
    # Преобразуем имена лейблов в ID
    label_ids = []
    if labels:
        for label_name in labels:
            label_id = get_or_create_label(label_name)
            if label_id:
                label_ids.append(label_id)
    
    # Валидация формата дат
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$", cve_data["published"]):
        cve_data["published"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$", cve_data["modified"]):
        cve_data["modified"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    
    query = """
    mutation VulnerabilityAdd($input: VulnerabilityAddInput!) {
        vulnerabilityAdd(input: $input) {
            id
            name
            description
            x_opencti_cvss_base_score
            x_opencti_cvss_base_severity
            x_opencti_cvss_attack_vector
            x_opencti_cvss_integrity_impact
            x_opencti_cvss_availability_impact
            x_opencti_cvss_confidentiality_impact
            created
            modified
            externalReferences {
                edges {
                    node {
                        id
                        source_name
                        url
                    }
                }
            }
            objectLabel {
                id
                value
                color
            }
        }
    }
    """
    
    variables = {
        "input": {
            "name": cve_data["name"],
            "description": cve_data["description"],
            "x_opencti_cvss_base_score": cve_data["base_score"],
            "x_opencti_cvss_base_severity": cve_data["base_severity"],
            "x_opencti_cvss_attack_vector": cve_data["attack_vector"],
            "x_opencti_cvss_integrity_impact": cve_data["integrity_impact"],
            "x_opencti_cvss_availability_impact": cve_data["availability_impact"],
            "x_opencti_cvss_confidentiality_impact": cve_data["confidentiality_impact"],
            "created": cve_data["published"],
            "modified": cve_data["modified"],
            "externalReferences": [external_ref_id],
            "objectLabel": label_ids
        }
    }
    
    result = execute_graphql(query, variables)
    if not result or "vulnerabilityAdd" not in result:
        return None
        
    vuln = result["vulnerabilityAdd"]
    logger.info(f"[OK] Created Vulnerability: {vuln['id']} for {cve_data['name']}")
    return vuln

def create_vulnerability_minimal(cve_id, external_ref_id=None, labels=None, description=None):
    """Создает объект Vulnerability с минимальными данными (когда NVD недоступен)"""
    # Преобразуем имена лейблов в ID
    label_ids = []
    if labels:
        for label_name in labels:
            label_id = get_or_create_label(label_name)
            if label_id:
                label_ids.append(label_id)
    
    # Используем текущее время для дат
    current_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Создаем базовое описание, если не передано
    if not description:
        description = f"Vulnerability {cve_id} identified through PoC repository analysis. No NVD data available."
    
    query = """
    mutation VulnerabilityAdd($input: VulnerabilityAddInput!) {
        vulnerabilityAdd(input: $input) {
            id
            name
            description
            created
            modified
            externalReferences {
                edges {
                    node {
                        id
                        source_name
                        url
                    }
                }
            }
            objectLabel {
                id
                value
                color
            }
        }
    }
    """
    
    variables = {
        "input": {
            "name": cve_id,
            "description": description,
            "created": current_time,
            "modified": current_time,
            "objectLabel": label_ids
        }
    }
    
    # Добавляем External Reference, если передан
    if external_ref_id:
        variables["input"]["externalReferences"] = [external_ref_id]
    
    result = execute_graphql(query, variables)
    if not result or "vulnerabilityAdd" not in result:
        return None
        
    vuln = result["vulnerabilityAdd"]
    logger.info(f"[OK] Created minimal Vulnerability: {vuln['id']} for {cve_id}")
    return vuln

def create_relation(from_id, to_id, relationship_type, identity_id, description=None):
    """Создает отношение между объектами"""
    query = """
    mutation RelationAdd($input: StixCoreRelationshipAddInput!) {
        stixCoreRelationshipAdd(input: $input) {
            id
            standard_id
            entity_type
            from {
                ... on BasicObject {
                    id
                }
            }
            to {
                ... on BasicObject {
                    id
                }
            }
            relationship_type
        }
    }
    """
    
    input_data = {
        "fromId": from_id,
        "toId": to_id,
        "relationship_type": relationship_type,
        "createdBy": identity_id
    }
    
    if description:
        input_data["description"] = description
    
    variables = {"input": input_data}
    
    result = execute_graphql(query, variables)
    if not result or "stixCoreRelationshipAdd" not in result:
        return None
        
    relation = result["stixCoreRelationshipAdd"]
    logger.info(f"[OK] Created Relation: {relation['id']} ({relationship_type})")
    return relation

def create_tool_and_vulnerability_for_empty_repo(cve_id, repository_url, pub_date, original_description, repo_path=None):
    """Создает Tool и Vulnerability для пустого репозитория (без артефактов) и прикрепляет архив репозитория"""
    try:
        logger.info(f"[EMPTY_REPO] Creating Tool and Vulnerability for empty repository {cve_id}")
        
        # Извлекаем owner из URL
        parts = repository_url.replace("https://github.com/", "").split("/")
        if len(parts) < 2:
            logger.error(f"[ERROR] Invalid GitHub URL: {repository_url}")
            return False
        
        owner, repo = parts[0], parts[1]
        
        # Создаем Identity (автор репозитория)
        identity = create_identity(owner, cve_id)
        identity_id = identity["id"] if identity else None
        
        # Создаем External Reference (ссылка на репозиторий)
        ext_ref_description = f"GitHub repository for {cve_id} PoC. Published: {pub_date}. Note: Repository contains only documentation/media files."
        external_ref = create_external_reference(repository_url, ext_ref_description)
        external_ref_id = external_ref["id"] if external_ref else None
        
        # Создаем Tool с дополнительным лейблом для пустого репозитория
        # Сначала получаем или создаем специальный лейбл
        empty_repo_label_id = get_or_create_label("Empty Repository", "#9E9E9E")  # Серый цвет
        
        # Создаем Tool с дополнительным лейблом
        tool = create_tool(cve_id, repository_url, original_description, None, identity_id, external_ref_id, [empty_repo_label_id])
        
        if not tool:
            logger.error(f"[ERROR] Failed to create Tool for empty repository {cve_id}")
            return False
        
        tool_id = tool["id"]
        logger.info(f"[OK] Created Tool for empty repository {cve_id}")
        
        # Получаем данные CVE из NVD
        logger.info(f"[NVD] Getting CVE data for {cve_id}")
        # Добавляем задержку для соблюдения rate limit NVD API
        time.sleep(NVD_API_REQUEST_DELAY)
        cve_data = get_cve_data_from_nvd(cve_id, NVD_API_MAX_RETRIES, NVD_API_BASE_DELAY)
        
        # Создаем Vulnerability - с данными из NVD или минимальную
        vulnerability = None
        vuln_id = None
        
        if cve_data:
            # Создаем External Reference для CVE
            cve_ref = create_cve_external_reference(cve_id)
            cve_ref_id = cve_ref["id"] if cve_ref else None
            
            # Создаем Vulnerability с полными данными из NVD
            vuln_labels = ["PoC", "Awaiting Analysis", cve_id, "Empty Repository"]
            vulnerability = create_vulnerability_from_nvd(cve_data, cve_ref_id, labels=vuln_labels)
            
            if vulnerability:
                vuln_id = vulnerability["id"]
                logger.info(f"[OK] Created Vulnerability with NVD data for empty repository {cve_id}")
            else:
                logger.warning(f"[WARNING] Failed to create Vulnerability with NVD data for empty repository {cve_id}")
        else:
            logger.info(f"[INFO] No NVD data available for {cve_id}, creating minimal Vulnerability")
            
            # Создаем минимальную Vulnerability без данных из NVD
            vuln_labels = ["PoC", "Awaiting Analysis", cve_id, "Empty Repository", "No NVD Data"]
            description = f"Vulnerability {cve_id} identified through PoC repository analysis. Repository contains only documentation/media files. No NVD data available."
            
            vulnerability = create_vulnerability_minimal(cve_id, None, vuln_labels, description)
            
            if vulnerability:
                vuln_id = vulnerability["id"]
                logger.info(f"[OK] Created minimal Vulnerability for empty repository {cve_id} (no NVD data)")
            else:
                logger.warning(f"[WARNING] Failed to create minimal Vulnerability for empty repository {cve_id}")
        
        # Создаем связь между Tool и Vulnerability, если Vulnerability была создана
        if vulnerability and vuln_id:
            tool_vuln_relation = create_relation(
                tool_id, 
                vuln_id, 
                "targets", 
                identity_id,
                f"PoC tool targets {cve_id} vulnerability (empty repository - documentation only)"
            )
            
            if tool_vuln_relation:
                logger.info(f"[OK] Created relation between Tool and Vulnerability for empty repository {cve_id}")
            else:
                logger.warning(f"[WARNING] Failed to create relation for empty repository {cve_id}")
        else:
            logger.warning(f"[WARNING] Could not create relation - Vulnerability creation failed for empty repository {cve_id}")
        
        # Прикрепляем архив репозитория к карточке Tool (если есть путь к репозиторию)
        try:
            if repo_path and os.path.isdir(repo_path):
                # Создаем архив репозитория для прикрепления
                archive_path = os.path.join(WORK_DIR, f"{cve_id}_repository.tar.gz")
                try:
                    # Создаем tar.gz архив
                    result = subprocess.run([
                        'tar', '-czf', archive_path, '-C', WORK_DIR, cve_id
                    ], capture_output=True, text=True, timeout=60)
                    
                    if result.returncode == 0 and os.path.isfile(archive_path):
                        logger.info(f"[FILE] Attaching repository archive to Tool {tool_id}: {os.path.basename(archive_path)}")
                        attach_ok = attach_file_to_object(tool_id, archive_path)
                        if attach_ok:
                            # Удаляем архив после прикрепления
                            os.remove(archive_path)
                            logger.info(f"[CLEANUP] Removed archive after attachment: {archive_path}")
                        else:
                            logger.warning("[WARNING] Could not attach repository archive to Tool")
                    else:
                        logger.warning(f"[WARNING] Failed to create repository archive: {result.stderr}")
                except Exception as e:
                    logger.warning(f"[WARNING] Failed to create repository archive: {e}")
            else:
                logger.warning("[WARNING] Repository path is missing; skipping attachment to Tool")
        except Exception as e:
            logger.warning(f"[WARNING] Failed to attach repository to Tool: {e}")
        
        logger.info(f"[OK] Successfully processed empty repository {cve_id} (Tool + Vulnerability created)")
        return True
        
    except Exception as e:
        logger.error(f"[ERROR] Error processing empty repository {cve_id}: {str(e)}")
        return False

def create_artifact_with_tool_and_vulnerability_relation(file_path, description, hashes, cve_id, repository_url, pub_date, original_description, repo_path=None):
    """Создает Artifact, Tool и Vulnerability в OpenCTI и связывает их"""
    try:
        # Проверяем, что файл существует
        if not os.path.exists(file_path):
            logger.error(f"[ERROR] File not found: {file_path}")
            return None
        
        # Извлекаем owner из URL
        parts = repository_url.replace("https://github.com/", "").split("/")
        if len(parts) < 2:
            logger.error(f"[ERROR] Invalid GitHub URL: {repository_url}")
            return None
        
        owner, repo = parts[0], parts[1]
        
        # Создаем Identity (автор репозитория)
        identity = create_identity(owner, cve_id)
        identity_id = identity["id"] if identity else None
        
        # Создаем External Reference (ссылка на репозиторий)
        ext_ref_description = f"GitHub repository for {cve_id} PoC. Published: {pub_date}"
        external_ref = create_external_reference(repository_url, ext_ref_description)
        external_ref_id = external_ref["id"] if external_ref else None
        
        # Извлекаем версию инструмента
        tool_version = extract_tool_version(repository_url, cve_id)
        
        # Создаем Tool
        tool = create_tool(cve_id, repository_url, original_description, tool_version, identity_id, external_ref_id)
        
        if not tool:
            logger.error(f"[ERROR] Failed to create Tool for {cve_id}")
            return None
        
        tool_id = tool["id"]
        
        # Прикрепляем весь репозиторий к карточке Tool (если есть путь к репозиторию)
        try:
            if repo_path and os.path.isdir(repo_path):
                # Создаем архив репозитория для прикрепления
                archive_path = os.path.join(WORK_DIR, f"{cve_id}_repository.tar.gz")
                try:
                    # Создаем tar.gz архив
                    result = subprocess.run([
                        'tar', '-czf', archive_path, '-C', WORK_DIR, cve_id
                    ], capture_output=True, text=True, timeout=60)
                    
                    if result.returncode == 0 and os.path.isfile(archive_path):
                        logger.info(f"[FILE] Attaching repository archive to Tool {tool_id}: {os.path.basename(archive_path)}")
                        attach_ok = attach_file_to_object(tool_id, archive_path)
                        if attach_ok:
                            # Удаляем архив после прикрепления
                            os.remove(archive_path)
                            logger.info(f"[CLEANUP] Removed archive after attachment: {archive_path}")
                        else:
                            logger.warning("[WARNING] Could not attach repository archive to Tool")
                    else:
                        logger.warning(f"[WARNING] Failed to create repository archive: {result.stderr}")
                except Exception as e:
                    logger.warning(f"[WARNING] Failed to create repository archive: {e}")
            else:
                logger.warning("[WARNING] Repository path is missing; skipping attachment to Tool")
        except Exception as e:
            logger.warning(f"[WARNING] Failed to attach repository to Tool: {e}")
        
        # Получаем данные CVE из NVD
        logger.info(f"[NVD] Getting CVE data for {cve_id}")
        # Добавляем задержку для соблюдения rate limit NVD API
        time.sleep(NVD_API_REQUEST_DELAY)
        cve_data = get_cve_data_from_nvd(cve_id, NVD_API_MAX_RETRIES, NVD_API_BASE_DELAY)
        
        # Создаем Vulnerability - с данными из NVD или минимальную
        vulnerability = None
        vuln_id = None
        
        if cve_data:
            # Создаем External Reference для CVE
            cve_ref = create_cve_external_reference(cve_id)
            cve_ref_id = cve_ref["id"] if cve_ref else None
            
            # Создаем Vulnerability с полными данными из NVD
            vuln_labels = ["PoC", "Awaiting Analysis", cve_id]
            vulnerability = create_vulnerability_from_nvd(cve_data, cve_ref_id, labels=vuln_labels)
            
            if vulnerability:
                vuln_id = vulnerability["id"]
                logger.info(f"[OK] Created Vulnerability with NVD data for {cve_id}")
            else:
                logger.warning(f"[WARNING] Failed to create Vulnerability with NVD data for {cve_id}")
        else:
            logger.info(f"[INFO] No NVD data available for {cve_id}, creating minimal Vulnerability")
            
            # Создаем минимальную Vulnerability без данных из NVD
            vuln_labels = ["PoC", "Awaiting Analysis", cve_id, "No NVD Data"]
            description = f"Vulnerability {cve_id} identified through PoC repository analysis. No NVD data available."
            
            vulnerability = create_vulnerability_minimal(cve_id, None, vuln_labels, description)
            
            if vulnerability:
                vuln_id = vulnerability["id"]
                logger.info(f"[OK] Created minimal Vulnerability for {cve_id} (no NVD data)")
            else:
                logger.warning(f"[WARNING] Failed to create minimal Vulnerability for {cve_id}")
        
        # Создаем связь между Tool и Vulnerability, если Vulnerability была создана
        if vulnerability and vuln_id:
            tool_vuln_relation = create_relation(
                tool_id, 
                vuln_id, 
                "targets", 
                identity_id,
                f"PoC tool targets {cve_id} vulnerability"
            )
            
            if tool_vuln_relation:
                logger.info(f"[OK] Created relation between Tool and Vulnerability for {cve_id}")
            else:
                logger.warning(f"[WARNING] Failed to create relation for {cve_id}")
        else:
            logger.warning(f"[WARNING] Could not create relation - Vulnerability creation failed for {cve_id}")
        
        # Получаем или создаем лейблы для артефакта
        artifact_label_ids = []
        
        # Лейбл PoC (синий цвет)
        poc_label_id = get_or_create_label("PoC", "#2196F3")
        if poc_label_id:
            artifact_label_ids.append(poc_label_id)
        
        # Лейбл Awaiting Analysis (ярко желтый цвет)
        analysis_label_id = get_or_create_label("Awaiting Analysis", "#FFEB3B")
        if analysis_label_id:
            artifact_label_ids.append(analysis_label_id)
        
        # Лейбл CVE ID (уникальный цвет)
        cve_label_id = get_cve_label_id(cve_id)
        if cve_label_id:
            artifact_label_ids.append(cve_label_id)
        
        # Лейбл автора (уникальный цвет)
        author_label_id = get_author_label_id(owner)
        if author_label_id:
            artifact_label_ids.append(author_label_id)
        
        # Создаем Artifact с лейблами и автором
        operations = {
            "query": """
                mutation AddArtifact($file: Upload!, $description: String, $objectLabel: [String], $createdBy: String) {
                    artifactImport(file: $file, x_opencti_description: $description, objectLabel: $objectLabel, createdBy: $createdBy) {
                        id
                        standard_id
                        objectLabel {
                            id
                            value
                            color
                        }
                        createdBy {
                            id
                            name
                        }
                    }
                }
            """,
            "variables": {
                "description": description,
                "file": None,
                "objectLabel": artifact_label_ids,
                "createdBy": identity_id
            }
        }
        
        # Подготовка map для файла
        map_data = {"0": ["variables.file"]}
        
        # Получение имени файла
        file_name = os.path.basename(file_path)
        
        # Создание multipart запроса
        files = {
            "operations": (None, json.dumps(operations), "application/json"),
            "map": (None, json.dumps(map_data), "application/json"),
            "0": (file_name, open(file_path, "rb"), "application/octet-stream")
        }
        
        headers = {
            "Authorization": f"Bearer {OPENCTI_TOKEN}",
            "Accept": "application/json",
        }
        
        # Отправка запроса
        response = requests.post(
            f"{OPENCTI_URL}/graphql",
            files=files,
            headers=headers,
            verify=config['opencti']['ssl_verify']
        )
        
        logger.debug(f"Response: {response.status_code} - {response.text}")
        
        if response.status_code == 200:
            result = response.json()
            if "errors" in result:
                logger.error(f"[ERROR] Artifact creation failed: {json.dumps(result['errors'], indent=2)}")
                return None
            else:
                artifact_id = result["data"]["artifactImport"]["id"]
                logger.info(f"[OK] Artifact created successfully! ID: {artifact_id} with {len(artifact_label_ids)} labels")
                
                # Создаем связь между Artifact и Tool
                try:
                    artifact_tool_relation = create_relation(
                        artifact_id,
                        tool_id,
                        "related-to",
                        identity_id,
                        f"PoC artifact for {cve_id} tool"
                    )
                    if artifact_tool_relation:
                        logger.info(f"[OK] Created relation between artifact and tool for {cve_id}")
                    else:
                        logger.warning(f"[WARNING] Failed to create relation between artifact and tool")
                except Exception as e:
                    logger.warning(f"[WARNING] Failed to create relation: {e}")
                
                return artifact_id
        else:
            logger.error(f"[ERROR] Request failed with status {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"[ERROR] Error during artifact creation: {str(e)}")
        return None
    finally:
        # Закрываем файл, если он был открыт
        if 'files' in locals() and '0' in files and hasattr(files['0'][1], 'close'):
            files['0'][1].close()

def process_poc_item(entry, cache):
    """Обрабатывает одну запись PoC"""
    try:
        # Извлечение данных из RSS
        title = getattr(entry, 'title', 'Unknown')
        poc_url = getattr(entry, 'link', '')
        
        # Получаем оригинальное описание из RSS
        original_description = getattr(entry, 'description', '')
        if not original_description:
            original_description = title  # Используем заголовок, если описание пустое
        
        if not poc_url:
            logger.warning(f"[WARNING] No URL found for entry: {title}")
            return False
            
        pub_date = datetime(*entry.published_parsed[:6]).isoformat() if hasattr(entry, 'published_parsed') else datetime.now().isoformat()
        
        # Проверка кеша - используем URL как ключ для большей надежности
        cache_key = poc_url
        if cache_key in cache:
            cache_entry = cache[cache_key]
            status = cache_entry.get('status', 'processed')
            files_count = cache_entry.get('files_processed', 0)
            
            if status == 'no_suitable_files':
                tool_created = cache_entry.get('tool_created', False)
                if tool_created:
                    logger.info(f"[SKIP] Skipping empty repository: {title} (Tool+Vulnerability already created, no suitable files found)")
                else:
                    logger.info(f"[SKIP] Skipping empty repository: {title} (no suitable files found, Tool+Vulnerability creation failed)")
            elif status == 'all_files_filtered':
                logger.info(f"[SKIP] Skipping filtered repository: {title} (all files were filtered out)")
            else:
                logger.info(f"[SKIP] Skipping processed item: {title} ({files_count} files processed)")
            return None
            
        # Извлечение CVE ID
        cve_id = extract_cve_id(title)
        if not cve_id:
            logger.warning(f"[WARNING] No CVE ID found in title: {title}")
            return False
        
        logger.info(f"[PROCESSING] Processing {cve_id}: {title}")
        
        # Проверяем, что это GitHub репозиторий
        if "github.com" not in poc_url:
            logger.warning(f"[WARNING] Skipping non-GitHub URL: {poc_url}")
            return False
        
        # Клонируем репозиторий и получаем файлы
        logger.info(f"[GIT] Cloning repository for {cve_id}")
        processed_files, repo_path = download_repository_files(poc_url, cve_id)
        
        if not processed_files:
            logger.info(f"[INFO] No files to process for {cve_id} after filtering (this is normal for repositories with only documentation/media files)")
            
            # Создаем Tool и Vulnerability для пустого репозитория
            logger.info(f"[EMPTY_REPO] Processing empty repository {cve_id} (creating Tool + Vulnerability without artifacts)")
            empty_repo_success = create_tool_and_vulnerability_for_empty_repo(cve_id, poc_url, pub_date, original_description, repo_path)
            
            # Чистим директорию CVE, если она была создана
            if repo_path and os.path.isdir(repo_path):
                remove_dir_quiet(repo_path)
            
            # Добавляем пустой репозиторий в кеш, чтобы не обрабатывать его повторно
            cache[cache_key] = {
                'processed_at': datetime.now().isoformat(),
                'cve_id': cve_id,
                'files_processed': 0,
                'status': 'no_suitable_files',
                'tool_created': empty_repo_success
            }
            
            if empty_repo_success:
                logger.info(f"[CACHE] Added empty repository {cve_id} to cache with Tool+Vulnerability (will be skipped in future runs)")
            else:
                logger.warning(f"[CACHE] Added empty repository {cve_id} to cache without Tool+Vulnerability due to errors (will be skipped in future runs)")
            
            return "no_suitable_files"
        
        logger.info(f"[OK] Processed {len(processed_files)} files for {cve_id}")
        
        # Обрабатываем каждый файл
        success_count = 0
        for file_info in processed_files:
            try:
                # Создание описания для артефакта (с дополнительной информацией)
                artifact_description = f"Proof-of-Concept for {cve_id}\n\nSource: {poc_url}\nPublished: {pub_date}\nFile: {file_info['file_name']}\n\nAutomatically imported from GitHub PoC repository"
                
                # Создание Artifact в OpenCTI с связью к Tool
                logger.info(f"[DIR] Creating artifact for {cve_id} - {file_info['file_name']}")
                artifact_id = create_artifact_with_tool_and_vulnerability_relation(
                    file_info['local_path'], 
                    artifact_description, 
                    file_info['hashes'], 
                    cve_id,
                    poc_url,
                    pub_date,
                    original_description,  # Передаем оригинальное описание для Tool
                    repo_path              # Путь к репозиторию для прикрепления к Tool
                )
                
                if artifact_id:
                    success_count += 1
                    logger.info(f"[OK] Successfully processed {cve_id} - {file_info['file_name']}")
                    # Удаляем локальный файл после успешной загрузки
                    remove_file_quiet(file_info.get('local_path'))
                else:
                    logger.error(f"[ERROR] Failed to create artifact for {cve_id} - {file_info['file_name']}")
                    
            except Exception as e:
                logger.error(f"[ERROR] Error processing file {file_info.get('file_name', 'unknown')}: {e}")
        
        # По завершении обработки очищаем директорию CVE
        if repo_path and os.path.isdir(repo_path):
            remove_dir_quiet(repo_path)
        
        if success_count > 0:
            cache[cache_key] = {
                'processed_at': datetime.now().isoformat(),
                'cve_id': cve_id,
                'files_processed': success_count,
                'status': 'processed'
            }
            logger.info(f"[OK] Successfully processed {cve_id} ({success_count}/{len(processed_files)} files)")
            return True
        else:
            # Если файлы найдены, но не прошли фильтрацию - это не ошибка
            if processed_files:
                logger.info(f"[INFO] No suitable files found for {cve_id} (all {len(processed_files)} files were filtered out)")
                # Добавляем в кеш репозиторий с отфильтрованными файлами
                cache[cache_key] = {
                    'processed_at': datetime.now().isoformat(),
                    'cve_id': cve_id,
                    'files_processed': 0,
                    'status': 'all_files_filtered'
                }
                logger.info(f"[CACHE] Added filtered repository {cve_id} to cache (will be skipped in future runs)")
                return "no_suitable_files"
            else:
                logger.error(f"[ERROR] Failed to process any files for {cve_id}")
                return False
        
    except Exception as e:
        logger.error(f"[ERROR] Error processing item {getattr(entry, 'title', 'unknown')}: {e}")
        return False

def remove_file_quiet(file_path: str) -> None:
    try:
        if file_path and os.path.isfile(file_path):
            os.remove(file_path)
            logger.info(f"[CLEANUP] Removed file: {file_path}")
    except Exception as e:
        logger.debug(f"[CLEANUP] Could not remove file {file_path}: {e}")

def remove_dir_quiet(dir_path: str) -> None:
    try:
        if dir_path and os.path.isdir(dir_path):
            shutil.rmtree(dir_path)
            logger.info(f"[CLEANUP] Removed directory: {dir_path}")
    except Exception as e:
        logger.debug(f"[CLEANUP] Could not remove directory {dir_path}: {e}")



def test_nvd_integration():
    """Тестирует интеграцию с NVD API"""
    logger.info("[TEST] Testing NVD API integration...")
    
    # Тестовый CVE ID
    test_cve = "CVE-2021-44228"
    
    # Получаем данные CVE
    cve_data = get_cve_data_from_nvd(test_cve, NVD_API_MAX_RETRIES, NVD_API_BASE_DELAY)
    if cve_data:
        logger.info(f"[TEST] Successfully retrieved CVE data for {test_cve}")
        logger.info(f"[TEST] Description: {cve_data['description'][:100]}...")
        logger.info(f"[TEST] Base Score: {cve_data['base_score']}")
        logger.info(f"[TEST] Severity: {cve_data['base_severity']}")
        return True
    else:
        logger.error(f"[TEST] Failed to retrieve CVE data for {test_cve}")
        return False

def single_check():
    """Выполняет однократную проверку RSS-ленты"""
    logger.info("[CHECK] Starting single check mode")
    logger.info("[INFO] Remember: 'No suitable files' is normal for repositories with only documentation/media files")
    
    cache = load_cache()
    
    try:
        # Загрузка и парсинг RSS
        logger.info(f"[RSS] Fetching RSS feed from: {POC_RSS_URL}")
        response = requests.get(POC_RSS_URL, timeout=30, allow_redirects=True)
        response.raise_for_status()
        
        feed = feedparser.parse(response.content)
        
        if not feed.entries:
            logger.warning("[WARNING] No entries found in RSS feed")
            return
        
        total_entries = len(feed.entries)
        logger.info(f"[STATS] Loaded RSS feed with {total_entries} entries")
        
        # Обработка элементов
        success_count = 0
        error_count = 0
        skip_count = 0
        no_suitable_files_count = 0
        
        for i, entry in enumerate(feed.entries, 1):
            try:
                logger.info(f"[PROGRESS] Progress: {i}/{total_entries} ({(i/total_entries)*100:.1f}%)")
                
                result = process_poc_item(entry, cache)
                if result is True:
                    success_count += 1
                elif result is False:
                    error_count += 1
                elif result == "no_suitable_files":
                    no_suitable_files_count += 1
                elif result is None:
                    skip_count += 1
                    
            except Exception as e:
                error_count += 1
                logger.error(f"[ERROR] Error processing entry {i}: {e}")
        
        # Финальная статистика
        logger.info("="*60)
        logger.info("[STATS] FINAL STATISTICS:")
        logger.info(f"[OK] Successfully processed: {success_count}")
        logger.info(f"[INFO] No suitable files: {no_suitable_files_count} (normal filtering)")
        logger.info(f"[SKIP] Skipped: {skip_count}")
        logger.info(f"[ERROR] Errors: {error_count}")
        if no_suitable_files_count > 0:
            logger.info(f"[INFO] Note: 'No suitable files' means repositories contain only documentation/media files (this is normal)")
        logger.info(f"[PROGRESS] Success rate: {(success_count/max(1, total_entries - skip_count))*100:.1f}%")
        logger.info("="*60)
    
    except requests.exceptions.RequestException as e:
        logger.error(f"[ERROR] Network error fetching RSS: {e}")
    except Exception as e:
        logger.error(f"[ERROR] Unexpected error: {e}")
    finally:
        logger.info("[SAVE] Saving cache...")
        save_cache(cache)
        logger.info("[FINISH] PoC RSS loader finished!")

def bootstrap_processing(count: int) -> None:
    """Обрабатывает последние N элементов RSS один раз (без фильтра по времени), затем завершает."""
    try:
        logger.info(f"[BOOTSTRAP] Processing latest {count} entries before switching to realtime...")
        logger.info("[INFO] Remember: 'No suitable files' is normal for repositories with only documentation/media files")
        
        response = requests.get(POC_RSS_URL, timeout=30, allow_redirects=True)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if not feed.entries:
            logger.warning("[BOOTSTRAP] No entries found in RSS feed")
            return
        # Берём последние N элементов (лента обычно уже отсортирована по дате)
        latest_entries = feed.entries[:max(0, count)]
        cache = load_cache()
        processed = 0
        no_suitable_files = 0
        for i, entry in enumerate(latest_entries, 1):
            try:
                logger.info(f"[BOOTSTRAP] {i}/{len(latest_entries)}")
                result = process_poc_item(entry, cache)
                if result is True:
                    processed += 1
                elif result == "no_suitable_files":
                    no_suitable_files += 1
            except Exception as e:
                logger.error(f"[BOOTSTRAP] Error processing bootstrap entry {i}: {e}")
        save_cache(cache)
        logger.info(f"[BOOTSTRAP] Finished: processed {processed}/{len(latest_entries)} entries, {no_suitable_files} had no suitable files")
    except Exception as e:
        logger.error(f"[BOOTSTRAP] Failed to run bootstrap phase: {e}")

def continuous_monitoring():
    """Непрерывный мониторинг RSS-ленты в режиме реального времени"""
    logger.info("[PROCESSING] Starting continuous RSS monitoring...")
    logger.info(f"[RSS] Monitoring URL: {POC_RSS_URL}")
    logger.info(f"[TIME] Check interval: {CHECK_INTERVAL} seconds ({CHECK_INTERVAL/60:.1f} minutes)")
    logger.info("[STOP] Press Ctrl+C to stop monitoring")
    logger.info("[INFO] Remember: 'No suitable files' is normal for repositories with only documentation/media files")
    
    # Загружаем кеш
    cache = load_cache()
    last_check_time = datetime.now()
    
    # Счетчики статистики
    total_checks = 0
    total_new_items = 0
    total_processed = 0
    total_errors = 0
    total_no_suitable_files = 0
    
    try:
        while True:
            try:
                current_time = datetime.now()
                total_checks += 1
                
                logger.info("="*80)
                logger.info(f"[SEARCH] RSS Check #{total_checks} - {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info(f"[TIMER] Time since last check: {current_time - last_check_time}")
                logger.info("="*80)
                

                
                # Загружаем и парсим RSS
                logger.info(f"[RSS] Fetching RSS feed from: {POC_RSS_URL}")
                response = requests.get(POC_RSS_URL, timeout=30, allow_redirects=True)
                response.raise_for_status()
                
                feed = feedparser.parse(response.content)
                
                if not feed.entries:
                    logger.warning("[WARNING] No entries found in RSS feed")
                    last_check_time = current_time
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                total_entries = len(feed.entries)
                logger.info(f"[STATS] Loaded RSS feed with {total_entries} entries")
                logger.info(f"[CACHE] Current cache size: {len(cache)} items")
                
                # Обработка новых элементов
                new_items_count = 0
                processed_count = 0
                error_count = 0
                no_suitable_files_count = 0
                
                for i, entry in enumerate(feed.entries, 1):
                    try:
                        title = getattr(entry, 'title', 'Unknown')
                        poc_url = getattr(entry, 'link', '')
                        logger.info(f"[PROGRESS] Processing entry {i}/{total_entries}: {title[:50]}...")
                        
                        result = process_poc_item(entry, cache)
                        if result is True:
                            processed_count += 1
                            new_items_count += 1
                            logger.info(f"[OK] Entry {i} processed successfully")
                        elif result is False:
                            error_count += 1
                            logger.error(f"[ERROR] Entry {i} failed to process")
                        elif result == "no_suitable_files":
                            no_suitable_files_count += 1
                            logger.info(f"[INFO] Entry {i} has no suitable files (normal filtering)")
                        elif result is None:
                            # Запись была пропущена (уже обработана или не подходит)
                            logger.info(f"[SKIP] Entry {i} was skipped (already processed or not suitable)")
                        
                    except Exception as e:
                        error_count += 1
                        logger.error(f"[ERROR] Error processing entry {i}: {e}")
                
                # Обновляем статистику
                total_new_items += new_items_count
                total_processed += processed_count
                total_errors += error_count
                total_no_suitable_files += no_suitable_files_count
                
                # Выводим статистику текущей проверки
                logger.info("="*60)
                logger.info("[STATS] CURRENT CHECK STATISTICS:")
                logger.info(f"[NEW] New items found: {new_items_count}")
                logger.info(f"[OK] Successfully processed: {processed_count}")
                logger.info(f"[INFO] No suitable files: {no_suitable_files_count} (normal filtering)")
                logger.info(f"[ERROR] Errors: {error_count}")
                if no_suitable_files_count > 0:
                    logger.info(f"[INFO] Note: 'No suitable files' means repositories contain only documentation/media files (this is normal)")
                logger.info("="*60)
                
                # Выводим общую статистику
                logger.info("[PROGRESS] OVERALL STATISTICS:")
                logger.info(f"[SEARCH] Total checks: {total_checks}")
                logger.info(f"[NEW] Total new items: {total_new_items}")
                logger.info(f"[OK] Total processed: {total_processed}")
                logger.info(f"[ERROR] Total errors: {total_errors}")
                logger.info(f"[STATS] Success rate: {(total_processed/max(1, total_new_items + no_suitable_files_count))*100:.1f}%")
                logger.info("="*60)
                
                # Сохраняем кеш
                save_cache(cache)
                
                # Обновляем время последней проверки
                last_check_time = current_time
                
                # Ждем до следующей проверки
                logger.info(f"[TIME] Waiting {CHECK_INTERVAL} seconds until next check...")
                logger.info(f"[TIME] Next check at: {(current_time + timedelta(seconds=CHECK_INTERVAL)).strftime('%Y-%m-%d %H:%M:%S')}")
                
                time.sleep(CHECK_INTERVAL)
                
            except requests.exceptions.RequestException as e:
                logger.error(f"[ERROR] Network error during RSS check: {e}")
                logger.info(f"[TIME] Retrying in {RETRY_DELAY} seconds...")
                time.sleep(RETRY_DELAY)
                
            except KeyboardInterrupt:
                logger.info("[STOP] Received interrupt signal, stopping monitoring...")
                break
                
            except Exception as e:
                logger.error(f"[ERROR] Unexpected error during monitoring: {e}")
                logger.info(f"[TIME] Retrying in {RETRY_DELAY} seconds...")
                time.sleep(RETRY_DELAY)
    
    except KeyboardInterrupt:
        logger.info("[STOP] Monitoring stopped by user")
    finally:
        # Финальная статистика
        logger.info("="*80)
        logger.info("[STATS] FINAL MONITORING STATISTICS:")
        logger.info(f"[SEARCH] Total checks performed: {total_checks}")
        logger.info(f"[NEW] Total new items found: {total_new_items}")
        logger.info(f"[OK] Total items processed: {total_processed}")
        logger.info(f"[INFO] Total no suitable files: {total_no_suitable_files} (normal filtering)")
        logger.info(f"[ERROR] Total errors: {total_errors}")
        if total_no_suitable_files > 0:
            logger.info(f"[INFO] Note: 'No suitable files' means repositories contain only documentation/media files (this is normal)")
        logger.info(f"[STATS] Overall success rate: {(total_processed/max(1, total_new_items + total_no_suitable_files))*100:.1f}%")
        logger.info("="*80)
        
        # Сохраняем кеш
        logger.info("[SAVE] Saving final cache...")
        save_cache(cache)
        logger.info("[FINISH] Continuous monitoring finished!")

def print_logging_info():
    """Выводит информацию о системе логирования"""
    logger.info("="*80)
    logger.info("[INFO] LOGGING SYSTEM INFORMATION:")
    logger.info("[INFO] The following log levels indicate different situations:")
    logger.info("[INFO] - [INFO] Normal operations, including 'no suitable files' (this is normal)")
    logger.info("[INFO] - [WARNING] Potential issues that don't stop processing")
    logger.info("[INFO] - [ERROR] Actual errors that prevent processing")
    logger.info("[INFO] - [DEBUG] Detailed information for troubleshooting")
    logger.info("[INFO] Note: 'No suitable files' means repositories contain only documentation/media files")
    logger.info("[INFO] This is normal behavior and not an error!")
    logger.info("="*80)

def main():
    """Основная функция"""
    import argparse
    
    # Объявляем глобальную переменную в начале функции
    global CHECK_INTERVAL
    
    # Парсинг аргументов командной строки
    parser = argparse.ArgumentParser(description='PoC RSS Loader for OpenCTI - Linux Version')
    # Continuous mode is default now
    parser.add_argument('--interval', '-i', type=int, default=CHECK_INTERVAL,
                       help=f'Check interval in seconds (default: {CHECK_INTERVAL})')
    parser.add_argument('--once', '-o', action='store_true',
                       help='Run once and exit (single check)')
    parser.add_argument('--bootstrap-count', type=int, default=BOOTSTRAP_COUNT,
                       help=f'Process latest N entries before realtime (default: {BOOTSTRAP_COUNT})')
    parser.add_argument('--test-nvd', action='store_true',
                       help='Test NVD API integration')
    parser.add_argument('--test-git', action='store_true',
                       help='Test git installation')
    
    args = parser.parse_args()
    
    # Обновляем интервал проверки если указан
    if args.interval != CHECK_INTERVAL:
        CHECK_INTERVAL = args.interval
        logger.info(f"[TIME] Updated check interval to {CHECK_INTERVAL} seconds")
    
    logger.info("[START] Starting PoC RSS loader for OpenCTI (Linux Version)")
    
    # Выводим информацию о системе логирования
    print_logging_info()
    
    # Проверяем установку git
    if not check_git_installed():
        logger.error("[ERROR] Git is required but not installed. Please install git and try again.")
        sys.exit(1)
    

    
    # Тестирование NVD интеграции
    if args.test_nvd:
        logger.info("="*60)
        logger.info("[TEST] TESTING NVD API INTEGRATION")
        logger.info("="*60)
        test_nvd_integration()
        return
    
    # Тестирование git
    if args.test_git:
        logger.info("="*60)
        logger.info("[TEST] TESTING GIT INSTALLATION")
        logger.info("="*60)
        if check_git_installed():
            logger.info("[OK] Git is properly installed and accessible")
        else:
            logger.error("[ERROR] Git test failed")
        return
    
    if args.once:
        # Запуск в режиме однократной проверки
        logger.info("[CHECK] Starting single check mode")
        single_check()
    else:
        # Bootstrap: обработка последних N элементов
        if args.bootstrap_count and args.bootstrap_count > 0:
            bootstrap_processing(args.bootstrap_count)
        # Запуск в режиме непрерывного мониторинга (по умолчанию)
        logger.info("[PROCESSING] Starting continuous monitoring mode")
        continuous_monitoring()

if __name__ == "__main__":
    main()
