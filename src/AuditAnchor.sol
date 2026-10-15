// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

/// @notice Publishes successive heads of one global audit chain, so a verifier can detect a
///         rewrite of the local log after that head was recorded.
/// @dev Holds no funds, makes no external call, and has no upgrade path. Only the owner may
///      publish, and each publish must advance the entry count. The latest anchor is in storage;
///      every earlier anchor stays readable from the Anchored event, which is the history.
contract AuditAnchor {
    /// @notice Address allowed to publish. Fixed at deployment and never reassigned.
    address public immutable owner;

    /// @notice The most recently published anchor.
    bytes32 public latestHead;
    uint64 public latestEntries;
    uint32 public latestVersion;
    uint256 public latestChainId;

    error NotOwner();
    error InvalidAnchor();

    /// @notice One published head. `chainId` and `version` bind it to a chain and an anchor
    ///         format, so a head from another deployment cannot be read as this chain's head.
    event Anchored(bytes32 indexed head, uint64 entries, uint32 version, uint256 chainId, address indexed publisher);

    constructor(address owner_) {
        if (owner_ == address(0)) revert InvalidAnchor();
        owner = owner_;
    }

    /// @notice Record the head of the audit chain after its first `entries` chained entries.
    /// @dev Fails closed on a zero head, a zero count, and a count that does not advance.
    function publish(bytes32 head, uint64 entries, uint32 version) external {
        if (msg.sender != owner) revert NotOwner();
        if (head == bytes32(0) || entries == 0) revert InvalidAnchor();
        if (entries <= latestEntries) revert InvalidAnchor();
        latestHead = head;
        latestEntries = entries;
        latestVersion = version;
        latestChainId = block.chainid;
        emit Anchored(head, entries, version, block.chainid, msg.sender);
    }
}
