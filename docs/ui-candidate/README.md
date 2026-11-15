# 차량 앱 UI 개선 후보

2026-11-15 개발 트랙에서 기존 협력 주차 화면의 가독성과 조작 흐름을 개선했습니다. 이 폴더는 개선 후보의 변경 내용과 검증 기록입니다. **저장소 루트의 Android 소스와 `apk/`의 서명 release는 이전 UI를 유지합니다.** 아래 변경분은 기본 소스에 적용하지 않았습니다.

## 변경 내용

큰 주차 배치와 선택 차량 정보를 좌우로 배치하고, 현재 단계·다음 행동·관제 연결을 큰 한글로 표시했습니다. 파란색 내 차, 초록색 주변 차량과 점선 경로를 구분하며, 시험용 차량 대리 응답과 장애물 시험·상세 상태는 기본적으로 접었습니다. 실제 지리 지도는 연결하지 않았습니다.

## 재현용 변경분

기준은 공개 제출 소스의 커밋 `7ebda6c3c7db156ee96675793f3fe54ff8bc0e93`입니다. [changes.patch](changes.patch)는 `CurbActivity.kt`와 `CurbRoadView.kt`만 변경합니다. 별도 작업 복사본에서 적용 후 빌드할 수 있습니다.

```bash
git apply --check docs/ui-candidate/changes.patch
git apply docs/ui-candidate/changes.patch
cd android
gradle -PpleosEnabled=true -PsubmissionDemo=true :app:assembleRelease :app:testReleaseUnitTest
```

서명 설정이 없는 release 결과물은 미서명 후보입니다. `apk/`의 기존 서명 파일과 같은 실행본으로 간주하거나 미서명 결과물로 덮어쓰면 안 됩니다. 이 자료에는 개발용/미서명 APK나 서명 키를 포함하지 않았습니다.

## 검증 범위

Pleos 에뮬레이터 API34 ARM64, 2560×1440에서 개발용 앱 `io.swarm.mobility`를 직접 설치·실행했습니다. 개발용 및 제출용 구성의 자동 검사 각 64개가 통과했으며 같은 검사 세트의 두 구성 실행입니다.

5대 개별 동의, 명시적 시작 전 이동 0, 정상 완료, 중단 후 위치 고정, 현재 위치 재계획·전원 동의 초기화, 재동의 전 시작 차단, 통신 연결 끊김 경고를 확인했습니다. 개발용 앱 충돌 기록은 발견되지 않았습니다. **새 UI의 release는 빌드 성공한 미서명 후보이며 서명·설치 검증 전입니다.** 실제 차량 제어·센서·자율주행 검증은 포함하지 않습니다.

[검증 기록](validation.json) · [화면과 역할 안내](../screen-guide.md)
