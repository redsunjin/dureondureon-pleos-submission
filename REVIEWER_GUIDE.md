# 평가자를 위한 실행 안내

심사 기준은 접수양식 기재 커밋 `a60c96191074050de562e4ef8d2d24f8cea3478b`입니다. [제출 스냅샷](https://github.com/redsunjin/dureondureon-pleos-submission/tree/a60c96191074050de562e4ef8d2d24f8cea3478b) 또는 [고정 태그](https://github.com/redsunjin/dureondureon-pleos-submission/tree/submission-2026-11-15)를 열면 제출 당시의 소스·APK·영상을 확인할 수 있습니다. 아래 안내는 제출 후 보강한 실행 설명입니다.

## 1 장비 없이 먼저 확인

[시연 영상](https://github.com/redsunjin/dureondureon-pleos-submission/blob/a60c96191074050de562e4ef8d2d24f8cea3478b/media/dureondureon-demo-draft.mp4)은 160초 무음 영상으로, 특정 가상 사례의 계산 재생과 기능 설명을 담았습니다. 제출 APK의 전체 실행 녹화나 실차 주행 영상은 아닙니다.

소스를 내려받은 뒤 저장소 루트에서 다음 명령으로 오프라인 재생 파일을 만들 수 있습니다. Python 표준 라이브러리만 사용하며 이 경로에는 Android SDK·계정·지도 키가 필요 없습니다.

```bash
python3 simulator/curb_replay.py --output work/replay.html
```

생성된 `work/replay.html`을 브라우저로 열어 협력·주변 차량 이동 없음·거부·중단 사례를 비교합니다. 이 명령은 가상 계산 결과를 재생합니다. 개발 기록은 Python 3.14이며, 제출 후 재현 확인에는 Python 3.12를 사용했습니다.

## 2 제출 APK를 Pleos에서 실행

환경은 Pleos Connect 2.1.0, API34 ARM64, 2560×1440입니다. 일반 스마트폰과 실제 차량에서 실행 가능하다고 검증한 것은 아닙니다. Android/Pleos 개발 환경과 `adb`가 필요합니다.

1. [서명된 제출 APK](https://raw.githubusercontent.com/redsunjin/dureondureon-pleos-submission/a60c96191074050de562e4ef8d2d24f8cea3478b/apk/redsunjin_%EB%91%90%EB%9F%B0%EB%91%90%EB%9F%B0_release.apk)를 다운로드합니다. GitHub 파일 보기 대신 이 직접 내려받기 링크를 사용할 수 있습니다.
2. 저장소 루트에서 서버를 실행하고 터미널을 유지합니다.

```bash
python3 -m coordination.server --port 8765 --database work/submission-demo.sqlite3
```

3. 별도 터미널에서 `adb devices`로 에뮬레이터 번호를 확인하고 아래 `<EMULATOR_SERIAL>`을 교체합니다.

```bash
adb devices
adb -s <EMULATOR_SERIAL> reverse tcp:8765 tcp:8765
adb -s <EMULATOR_SERIAL> install -r apk/redsunjin_두런두런_release.apk
adb -s <EMULATOR_SERIAL> shell am start -n io.swarm.mobility.submission/io.swarm.mobility.MainActivity
```

저장소 밖에 APK를 내려받았다면 설치 명령의 파일 경로만 실제 위치로 바꿉니다. 패키지 이름은 `io.swarm.mobility.submission`입니다.

4. 앱의 **협력 평행주차 열기** 버튼을 열고 **초기 배치로 새 계획**을 누릅니다. 해당 버튼이 화면 아래에 있으면 스크롤합니다. 주변 4대와 새 차량의 총 5개 응답을 시험 화면에서 각각 입력합니다.
5. 4/5 동의에서는 시작할 수 없고 5/5가 된 뒤 별도 시작 버튼을 눌러야 진행됩니다. 공간 확보 → 후진 진입 → 정렬 완료를 확인합니다.
6. 확인이 끝나면 서버 터미널에서 `Ctrl+C`로 종료하고 `adb -s <EMULATOR_SERIAL> reverse --remove tcp:8765`로 연결을 해제합니다.

현재 제출 APK의 실제 캡처는 [완료 화면](media/signed-release-completed.png)입니다. 밝은 배치 UI의 별도 개선 후보와 모양이 다릅니다.

## 연결되지 않을 때

| 증상 | 확인할 내용 |
| --- | --- |
| 관제 연결 끊김 | PC 서버 터미널, 에뮬레이터 번호와 `adb reverse` 설정 확인 |
| 시작 버튼 비활성 | 5대 응답·유효한 계획·최신 연결 상태와 시작 불가 이유 확인 |
| 실제 주차 지도 없음 | 제출 프로필은 지리 지도 기능을 비활성화한 가상 협력 주차 시연 |
| 설치 시 서명 충돌 | 같은 패키지에 다른 서명의 기존 앱이 있는지 확인. 필요하면 별도 시험 에뮬레이터 사용 |
| 빌드 준비 부족 | 우선 제공된 서명 APK로 확인. 소스 빌드에는 별도 JDK/Gradle/Android/Pleos 의존성 필요 |

`adb reverse` 연결은 개발 환경의 로컬 통신입니다. 클라우드 서버 주소·회원가입·실제 차량 계정을 요구하지 않습니다. SDK나 지도 부가 기능의 모든 외부 통신을 차단한다고 보장하지는 않습니다.

## 검증 범위

제출 서명본의 직접 실행 확인은 설치·실행·연결·4/5 시작 차단·5/5 시작·정상 완료입니다. 철회·연결 유실·재계획의 자동 검사와 개선 개발용 UI의 직접 실행은 별도 증거로 구분합니다. [검증 기록](VALIDATION.md), [버전 구분](SUBMISSION.md), [로컬 시연 보안](SECURITY.md)을 참고하세요.
