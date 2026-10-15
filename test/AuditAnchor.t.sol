// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {AuditAnchor} from "../src/AuditAnchor.sol";

interface VmAuditAnchorTest {
    struct Log {
        bytes32[] topics;
        bytes data;
        address emitter;
    }

    function expectRevert() external;
    function expectEmit(bool checkTopic1, bool checkTopic2, bool checkTopic3, bool checkData, address emitter) external;
    function prank(address sender) external;
    function recordLogs() external;
    function getRecordedLogs() external returns (Log[] memory);
}

contract AuditAnchorTest {
    VmAuditAnchorTest private constant vm = VmAuditAnchorTest(address(uint160(uint256(keccak256("hevm cheat code")))));
    address private owner = address(0xA11CE);
    address private stranger = address(0xB0B);
    bytes32 private constant HEAD_ONE = keccak256("audit head one");
    bytes32 private constant HEAD_TWO = keccak256("audit head two");
    bytes32 private constant ANCHORED_TOPIC = keccak256("Anchored(bytes32,uint64,uint32,uint256,address)");

    AuditAnchor private anchor;

    function setUp() public {
        anchor = new AuditAnchor(owner);
    }

    function test_a_deployment_without_an_owner_is_refused() public {
        vm.expectRevert();
        new AuditAnchor(address(0));
    }

    function test_only_the_owner_can_publish() public {
        vm.prank(stranger);
        vm.expectRevert();
        anchor.publish(HEAD_ONE, 10, 1);
        require(anchor.latestEntries() == 0, "a stranger changed the anchor");
    }

    function test_publish_emits_head_entries_version_chain_and_publisher() public {
        vm.expectEmit(true, true, false, true, address(anchor));
        emit AuditAnchor.Anchored(HEAD_ONE, 10, 1, block.chainid, owner);
        vm.prank(owner);
        anchor.publish(HEAD_ONE, 10, 1);
    }

    function test_a_zero_head_is_refused() public {
        vm.prank(owner);
        vm.expectRevert();
        anchor.publish(bytes32(0), 10, 1);
    }

    function test_a_zero_entry_count_is_refused() public {
        vm.prank(owner);
        vm.expectRevert();
        anchor.publish(HEAD_ONE, 0, 1);
    }

    function test_a_count_that_does_not_advance_is_refused() public {
        vm.prank(owner);
        anchor.publish(HEAD_ONE, 10, 1);

        vm.prank(owner);
        vm.expectRevert();
        anchor.publish(HEAD_TWO, 10, 2);

        vm.prank(owner);
        vm.expectRevert();
        anchor.publish(HEAD_TWO, 5, 2);

        require(anchor.latestHead() == HEAD_ONE, "a refused publish changed the anchor");
    }

    function test_two_anchors_keep_the_latest_in_storage() public {
        vm.prank(owner);
        anchor.publish(HEAD_ONE, 10, 1);
        vm.prank(owner);
        anchor.publish(HEAD_TWO, 25, 2);

        require(anchor.latestHead() == HEAD_TWO, "latest head");
        require(anchor.latestEntries() == 25, "latest entries");
        require(anchor.latestVersion() == 2, "latest version");
        require(anchor.latestChainId() == block.chainid, "latest chain id");
        require(anchor.owner() == owner, "owner changed");
    }

    function test_each_publish_keeps_its_own_event() public {
        vm.recordLogs();
        vm.prank(owner);
        anchor.publish(HEAD_ONE, 10, 1);
        vm.prank(owner);
        anchor.publish(HEAD_TWO, 25, 2);

        VmAuditAnchorTest.Log[] memory logs = vm.getRecordedLogs();
        require(logs.length == 2, "expected two anchor events");
        require(logs[0].topics[0] == ANCHORED_TOPIC, "wrong event signature");
        require(logs[0].topics[1] == HEAD_ONE, "first event head");
        require(logs[1].topics[1] == HEAD_TWO, "second event head");
        require(logs[1].emitter == address(anchor), "wrong emitter");
    }
}
