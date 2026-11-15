package io.swarm.mobility
import org.junit.Assert.*
import org.junit.Test
class ParticipationPolicyTest {
    @Test fun invitationParsing() {
        val token="a".repeat(43)
        assertEquals(token,ParticipationPolicy.token("http://127.0.0.1:8770/participate/#$token"))
        assertEquals(token,ParticipationPolicy.token(token))
        for(value in listOf("",token+"?evil",token.drop(1),"http://example.com/without-fragment"))assertNull(ParticipationPolicy.token(value))
    }
    @Test fun noApprovalForTerminalStaleOrPendingResponses() {
        assertTrue(ParticipationPolicy.allowed("approve","awaiting","pending",0,false))
        for(status in listOf("ready","running","stopped","expired","rejected","completed"))assertFalse(ParticipationPolicy.allowed("approve",status,"pending",0,false))
        for(age in listOf(-1L,2001L,Long.MAX_VALUE))assertFalse(ParticipationPolicy.allowed("approve","awaiting","pending",age,false))
        assertFalse(ParticipationPolicy.allowed("approve","awaiting","pending",0,true))
    }
    @Test fun withdrawalNeedsApprovedActivePlan() {
        for(status in listOf("awaiting","ready","running"))assertTrue(ParticipationPolicy.allowed("withdraw",status,"approved",100,false))
        assertFalse(ParticipationPolicy.allowed("withdraw","stopped","approved",100,false))
        assertFalse(ParticipationPolicy.allowed("withdraw","awaiting","pending",100,false))
        assertFalse(ParticipationPolicy.allowed("start","ready","approved",100,false))
    }
}
