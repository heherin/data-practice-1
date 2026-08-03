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