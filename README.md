## 데이터 수집 미니 파이프라인

## 실습 준비

환경 ****준비

- **venv** 생성 ****및 ****활성화 ****후 ****필요한 ****패키지를 **requirements.txt**로 ****관리하여 ****설치

비동기 ****수집

- **asyncio + httpx**를 ****사용해 ****위 **3**개 **API**를 ****동시에 ****수집하는 ****파이프라인 ****작성
- **(asyncio.gather()** 활용**)**

스키마 ****검증

- 수집한 **JSON**에서 ****필요한 ****필드를 ****추출하여 **Pydantic v2** 모델로 ****타입**·**범위 ****검증

저장 ****및 ****성능 ****비교

- 검증 ****통과한 ****데이터를 **CSV**와 **Parquet** 두 ****형식으로 ****저장하고 ****읽기**/**쓰기 ****시간 ****측정**·**비교

테스트 ****및 **Git** 커밋

- **pytest**로 ****스키마 ****검증 ****테스트 ****작성**, ruff**로 ****코드 ****스타일 ****검사 ****결과 ****정리

---

## 환경 준비

### venv 생성 및 활성화

- 가상환경 생성

```python
python -m venv venv
```

- 가상환경 활성화

```python
souce venv/bin/activate
```

### 필요한 패키지를 requirements.txt로 관리하여 설치

```python
pip freeze > requirements.txt
```

- requirements.txt에 기록된 패키지 설치하기

```python
pip install -r requirements.txt
```

---

## 로깅 설정

- 프로그램 전체에 쓸 로거 준비
- INFO레벨 이상만 출력
- 시간, 레벨, 메시지 형식으로 찍힘

```python
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
```

---

## Pydantic 스키마 정의

- HourlyData, WeatherResponse: 날씨 API 응답 구조
    - time, temperature_2m, precipitation_probability는 리스트

```python
class HourlyData(BaseModel):
    time: list[str]
    temperature_2m: list[float] = Field(description="기온 (°C)")
    precipitation_probability: list[int] = Field(description="강수 확률 (%)")

class WeatherResponse(BaseModel):
    latitude: float
    longitude: float
    timezone: str
    elevation: float
    hourly: HourlyData
```

- CountryName, CountryInfo: 국가 API 응답 구조

```python
class CountryName(BaseModel):
    common: str
    official: str
    nativeName: dict[str, Any] | None = None  # 언어 코드가 동적으로 바뀌므로 Dict 처리

class CountryInfo(BaseModel):
    name: CountryName
    capital: list[str] | None = None
    population: int = Field(gt=0, description="인구수는 0 이상")
```

- IpApiSuccessResponse, ApiErrorResponse: IP-API는 성공, 실패 응답 모양이 다르니까 모델을 두개로 분리

```python
class IpApiSuccessResponse(BaseModel):
    status: Literal["success"]
    country: str
    countryCode: str
    query: str  # IP 주소

class ApiErrorResponse(BaseModel):
    status: Literal["fail"]
    message: str
```

---

## 요청할 URL 목록 정의

```python
urls = [
    "https://api.open-meteo.com/v1/forecast?latitude=37.5665&longitude=126.9780&hourly=temperature_2m,precipitation_probability&forecast_days=3&timezone=Asia/Seoul",
    "https://restcountries.com/v3.1/alpha/KOR",
    "http://ip-api.com/json/8.8.8.8",
]
```

## 개별 요청 하나 처리 - fetch()

- URL 하나에 대해 비동기로 GET 요청
- 성공하면 JSON을 파이썬 dict, list로 반환
- 네트워크 오류나 JSON 파싱 오류가 나면 에러 로그만 남기고 예외를 밖으로 던지지않고 None을 반환
    - 이래야 한 URL이 실패해도 나머지 요청들은 영향을 받지 않음

```python
async def fetch(client: httpx.AsyncClient, url: str) -> Any | None:
    try:
        response = await client.get(url, timeout=10.0)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"요청 실패 [{url}]: {e}")
        return None
```

## 받은 데이터 검증 - parse_and_validate()

- data가 None이면 바로 None 반환
- idx(URL의 위치)에 따라 어느 스키마로 검증할지 분기
- 검증 실패(ValidationError)하면 어떤 필드가 왜 실패했는지 로그 남기고 None 반환

```python
def parse_and_validate(idx: int, data: Any):
    if not data:
        return None

    try:
        if idx == 0:
            validated = WeatherResponse.model_validate(data)
            logger.info(f"[날씨 API 검증 성공] 위도: {validated.latitude}, 타임존: {validated.timezone}")
            return validated

        elif idx == 1:
            # v3.1 응답은 리스트 [ {...} ] 형태로 오므로 첫 번째 요소 추출
            country_data = data[0] if isinstance(data, list) else data
            validated = CountryInfo.model_validate(country_data)
            logger.info(f"[국가 API 검증 성공] 국가명: {validated.name.common}, 인구수: {validated.population:,}명")
            return validated

        elif idx == 2:
            status = data.get("status") if isinstance(data, dict) else None
            if status == "fail":
                validated = ApiErrorResponse.model_validate(data)
                logger.warning(f"[IP-API 에러 응답 수신] 메시지: {validated.message}")
            else:
                validated = IpApiSuccessResponse.model_validate(data)
                logger.info(f"[IP-API 성공 응답] 국가: {validated.country}, IP: {validated.query}")
            return validated

    except ValidationError as e:
        first_error = e.errors()[0]
        logger.error(f"[Index {idx} 검증 실패]: {first_error['msg']} (필드: {first_error['loc']})")
        return None
```

---

## 전체 파이프라인 조립 - collect_data()

- tasks = [fetch(client, url) for url in urls]
    - task 리스트 생성
    - fetch 호출하지만 await가 안 붙었으니까 객체 3개만 만들어짐
- asyncio.gather(*tasks)
    - 3개를 동시에 실행 시작
- result는 [날씨결과, 국가결과, IP결과] 순서로, 각 원소는 성공시 dict, 실패 시 None

```python
async def collect_data() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # (1) 비동기 API 요청
        tasks = [fetch(client, url) for url in urls]
        results = await asyncio.gather(*tasks)
```

- 각 결과를 순서대로 parse_and_validation에 넘김
- 검증까지 통과한 것만 .model_dump()(Pydantic 모델 → dict 변환)해서 valid_results에 쌓음

```python
        # (2) Pydantic v2 검증 수행 후 딕셔너리로 변환
        valid_results = []
        for idx, result in enumerate(results):
            if result:
                validated = parse_and_validate(idx, result)
                if validated:
                    # Pandas 변환을 위해 Pydantic 모델을 dict로 변환 (.model_dump())
                    valid_results.append(validated.model_dump())

        return valid_results
```

---

## 저장 및 성능 비교 - benchmark_storage()

- valid_results(검증 통과한 dict 리스트)를 pd.json_normalize로 중첩된 dict를 평평한 컬럼 구조로 펼쳐서 DataFrame으로 변환

```python
def benchmark_storage(data: list[dict[str, Any]]):
    df = pd.json_normalize(data)
		...
```

---

## 실행

- asyncio.run()이 이벤트 루프를 새로 만들어서 collect_data()를 실행시키는 진입점 역할
- 검증 통과한 데이터가 하나라도 있으면 저장, 벤치마크 진행

```python
if __name__ == "__main__":
    validated_data = asyncio.run(collect_data())
    if validated_data:
        benchmark_storage(validated_data)
```

---

## 전체 코드

```python
import asyncio
import logging
import time
from typing import Any, Literal

import httpx
import pandas as pd
from pydantic import BaseModel, Field, ValidationError

# =================================
# 로깅 설정
# =================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ==================================
# Pydantic v2 스키마 정의
# ==================================

# 1. 날씨 데이터 스키마
class HourlyData(BaseModel):
    time: list[str]
    temperature_2m: list[float] = Field(description="기온 (°C)")
    precipitation_probability: list[int] = Field(description="강수 확률 (%)")

class WeatherResponse(BaseModel):
    latitude: float
    longitude: float
    timezone: str
    elevation: float
    hourly: HourlyData

# 2. 국가 정보 데이터 스키마 (v3.1 규격 완벽 대응)
class CountryName(BaseModel):
    common: str
    official: str
    nativeName: dict[str, Any] | None = None  # 언어 코드가 동적으로 바뀌므로 Dict 처리

class CountryInfo(BaseModel):
    name: CountryName
    capital: list[str] | None = None
    population: int = Field(gt=0, description="인구수는 0 이상")

# 3. API 응답 스키마 (성공 / 에러 분리)
class IpApiSuccessResponse(BaseModel):
    status: Literal["success"]
    country: str
    countryCode: str
    query: str  # IP 주소

class ApiErrorResponse(BaseModel):
    status: Literal["fail"]
    message: str

# ==================================
# URL 목록 정의
# ==================================
urls = [
    "https://api.open-meteo.com/v1/forecast?latitude=37.5665&longitude=126.9780&hourly=temperature_2m,precipitation_probability&forecast_days=3&timezone=Asia/Seoul",
    "https://restcountries.com/v3.1/alpha/KOR",
    "http://ip-api.com/json/8.8.8.8",
]

# 단일 URL 비동기 요청 함수
async def fetch(client: httpx.AsyncClient, url: str) -> Any | None:
    try:
        response = await client.get(url, timeout=10.0)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"요청 실패 [{url}]: {e}")
        return None

# 파싱 및 검증 전담 함수
def parse_and_validate(idx: int, data: Any):
    if not data:
        return None

    try:
        if idx == 0:
            validated = WeatherResponse.model_validate(data)
            logger.info(f"[날씨 API 검증 성공] 위도: {validated.latitude}, 타임존: {validated.timezone}")
            return validated

        elif idx == 1:
            # v3.1 응답은 리스트 [ {...} ] 형태로 오므로 첫 번째 요소 추출
            country_data = data[0] if isinstance(data, list) else data
            validated = CountryInfo.model_validate(country_data)
            logger.info(f"[국가 API 검증 성공] 국가명: {validated.name.common}, 인구수: {validated.population:,}명")
            return validated

        elif idx == 2:
            status = data.get("status") if isinstance(data, dict) else None
            if status == "fail":
                validated = ApiErrorResponse.model_validate(data)
                logger.warning(f"[IP-API 에러 응답 수신] 메시지: {validated.message}")
            else:
                validated = IpApiSuccessResponse.model_validate(data)
                logger.info(f"[IP-API 성공 응답] 국가: {validated.country}, IP: {validated.query}")
            return validated

    except ValidationError as e:
        first_error = e.errors()[0]
        logger.error(f"[Index {idx} 검증 실패]: {first_error['msg']} (필드: {first_error['loc']})")
        return None

# 동시 수집 파이프라인
async def collect_data() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # (1) 비동기 API 요청
        tasks = [fetch(client, url) for url in urls]
        results = await asyncio.gather(*tasks)

        # (2) Pydantic v2 검증 수행 후 딕셔너리로 변환
        valid_results = []
        for idx, result in enumerate(results):
            if result:
                validated = parse_and_validate(idx, result)
                if validated:
                    # Pandas 변환을 위해 Pydantic 모델을 dict로 변환 (.model_dump())
                    valid_results.append(validated.model_dump())

        return valid_results

# ==================================
# CSV vs Parquet 저장 및 benchmark
# ==================================
def benchmark_storage(data: list[dict[str, Any]]):
    df = pd.json_normalize(data)

    csv_path = "validated_data.csv"
    parquet_path = "validated_data.parquet"

    # 1. CSV 쓰기 측정
    t0 = time.perf_counter()
    df.to_csv(csv_path, index=False)
    csv_write_time = time.perf_counter() - t0

    # 2. Parquet 쓰기 측정
    t0 = time.perf_counter()
    df.to_parquet(parquet_path, index=False, engine="pyarrow")
    parquet_write_time = time.perf_counter() - t0

    # 3. CSV 읽기 측정
    t0 = time.perf_counter()
    pd.read_csv(csv_path)
    csv_read_time = time.perf_counter() - t0

    # 4. Parquet 읽기 측정
    t0 = time.perf_counter()
    pd.read_parquet(parquet_path, engine="pyarrow")
    parquet_read_time = time.perf_counter() - t0

    print(f"쓰기 시간: {csv_write_time * 1000:8.3f} ms / {parquet_write_time * 1000:8.3f} ms")
    print(f"읽기 시간: {csv_read_time * 1000:8.3f} ms / {parquet_read_time * 1000:8.3f} ms")

if __name__ == "__main__":
    validated_data = asyncio.run(collect_data())
    if validated_data:
        benchmark_storage(validated_data)
```

---

## 전체 흐름

`URL 3개 동시 요청(gather)` → `각각 스키마로 검증` → `통과한 것만 dict로 모음` → `DataFrame으로 변환해 CSV/Parquet 저장·성능 비교`
