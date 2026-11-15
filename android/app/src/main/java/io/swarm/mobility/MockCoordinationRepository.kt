package io.swarm.mobility

data class Candidate(
    val spaceId: String,
    val eta: String,
    val availability: String,
    val reason: String,
    val warning: String? = null,
)

class MockCoordinationRepository {
    fun candidates(): List<Candidate> = listOf(
        Candidate("A-14", "약 1분", "여유 3칸", "입구와 가깝고 흐름 원활"),
        Candidate("B-03", "약 2분", "여유 6칸", "예약 충돌 없음", "B 교차로에서 앞 차량 통과 필요"),
        Candidate("C-21", "약 3분", "여유 9칸", "출차 방향과 일치", "점유 정보 8초 전 갱신"),
    )

    fun accept(candidate: Candidate): String =
        "${candidate.spaceId} 예약 lease 발급 · 45초 후 만료"
}
