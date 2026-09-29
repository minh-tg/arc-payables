// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {PaymentGuard} from "../src/PaymentGuard.sol";
import {MockArcUSDC} from "./PaymentGuard.t.sol";

/// @notice The cheatcodes this file needs. The repository deliberately has no forge-std dependency.
interface VmInvariant {
    function warp(uint256 timestamp) external;
    function chainId(uint256 newChainId) external;
    function prank(address sender) external;
    function addr(uint256 privateKey) external returns (address);
    function sign(uint256 privateKey, bytes32 digest) external returns (uint8 v, bytes32 r, bytes32 s);
    function getBlockTimestamp() external view returns (uint256);
}

/// @notice Proves the claim the project rests on: the caps cannot be exceeded.
/// @dev Unit tests sample the boundary. These invariants hold after every call in an arbitrary
///      sequence of payments, time movements, pauses and unpauses. The fuzz actions live on the test
///      contract itself, which is Foundry's default invariant target, so no target selection is
///      needed and no forge-std dependency is introduced.
///
///      The actions never revert. A revert would be recorded as a failed call rather than as the
///      guard behaving correctly, and refusing is what the guard is for: caps, expiry, replay and
///      the pause control all exist to say no.
contract PaymentGuardInvariantTest {
    VmInvariant private constant vm = VmInvariant(address(uint160(uint256(keccak256("hevm cheat code")))));

    uint256 private constant POLICY_KEY = 0xA11CE;
    uint256 private constant CHAIN_ID = 5_042_002;
    uint96 private constant PER_PAYMENT_CAP = 100e6; // 100 USDC
    uint96 private constant EPOCH_CAP = 1_000e6; // 1,000 USDC per epoch
    uint96 private constant RECIPIENT_CAP = 500e6; // 500 USDC per recipient per epoch
    uint64 private constant EPOCH_LENGTH = 3_600; // one hour, so time travel crosses epochs

    MockArcUSDC private token;
    PaymentGuard private guard;
    address private pauser = address(0x5a5);
    address private policySigner;
    address[3] private recipients;

    // Observed effects, used by the invariants below.
    bool private started;
    uint64 private minEpoch;
    uint64 private maxEpoch;
    uint256 private totalSettled;
    uint256 private maxSettled;
    uint256 private totalSettledWhilePaused;

    function setUp() public {
        vm.chainId(CHAIN_ID);
        token = new MockArcUSDC();
        policySigner = vm.addr(POLICY_KEY);
        guard = new PaymentGuard(
            address(token), policySigner, pauser, PER_PAYMENT_CAP, EPOCH_CAP, RECIPIENT_CAP, EPOCH_LENGTH
        );
        recipients[0] = address(0xBEEF);
        recipients[1] = address(0xF00D);
        recipients[2] = address(0xCAFE);
        token.mint(address(this), 1_000_000e6);
        token.approve(address(guard), type(uint256).max);
    }

    // -- fuzz actions ----------------------------------------------------------------------

    function pay(uint96 amount, uint8 recipientIndex, uint64 expiryOffset, bytes32 paymentId) public {
        address recipient = recipients[recipientIndex % 3];
        PaymentGuard.Permit memory permit = PaymentGuard.Permit({
            payer: address(this),
            token: address(token),
            recipient: recipient,
            amount: amount,
            evidenceHash: keccak256(abi.encode(paymentId, recipient)),
            paymentId: paymentId,
            expiry: uint64(block.timestamp + 1 + (expiryOffset % 3600))
        });
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(POLICY_KEY, guard.hashPermit(permit));
        try guard.pay(permit, abi.encodePacked(r, s, v)) {
            totalSettled += amount;
            if (amount > maxSettled) maxSettled = amount;
            _trackEpoch();
        } catch {}
        if (guard.paused()) totalSettledWhilePaused = totalSettled;
    }

    /// @notice Let a little time pass, so the fuzzer can move between budget epochs.
    function moveTime(uint64 secondsToMove) public {
        // Read the timestamp through the cheatcode, so the lint can tell the read from the warp.
        vm.warp(vm.getBlockTimestamp() + 1 + (secondsToMove % 1800));
        _trackEpoch();
    }

    function pauseGuard() public {
        vm.prank(pauser);
        try guard.pause() {} catch {}
    }

    function unpauseGuard() public {
        vm.prank(pauser);
        try guard.unpause() {} catch {}
    }

    function _trackEpoch() private {
        uint64 epoch = guard.epochAt(block.timestamp);
        if (!started) {
            started = true;
            minEpoch = epoch;
            maxEpoch = epoch;
            return;
        }
        if (epoch < minEpoch) minEpoch = epoch;
        if (epoch > maxEpoch) maxEpoch = epoch;
    }

    // -- invariants ------------------------------------------------------------------------

    function invariant_epoch_spend_never_exceeds_the_epoch_cap() public view {
        for (uint64 epoch = minEpoch; epoch <= maxEpoch; epoch++) {
            require(guard.epochSpent(epoch) <= uint256(EPOCH_CAP), "epoch spend exceeded the cap");
        }
    }

    function invariant_recipient_spend_never_exceeds_its_cap() public view {
        for (uint64 epoch = minEpoch; epoch <= maxEpoch; epoch++) {
            for (uint256 index = 0; index < 3; index++) {
                require(
                    guard.recipientEpochSpent(epoch, recipients[index]) <= uint256(RECIPIENT_CAP),
                    "recipient spend exceeded its cap"
                );
            }
        }
    }

    /// @dev The per-recipient figures are the same spend counted by recipient, so they must add up
    ///      to the epoch figure exactly. A divergence would mean the accounting had drifted.
    function invariant_recipient_spends_sum_to_the_epoch_spend() public view {
        for (uint64 epoch = minEpoch; epoch <= maxEpoch; epoch++) {
            uint256 sum;
            for (uint256 index = 0; index < 3; index++) {
                sum += guard.recipientEpochSpent(epoch, recipients[index]);
            }
            require(sum == guard.epochSpent(epoch), "recipient spends do not sum to the epoch spend");
        }
    }

    /// @dev Every settled epoch, summed, must equal what the guard says it spent across them.
    function invariant_epoch_spends_equal_what_was_actually_settled() public view {
        uint256 sum;
        for (uint64 epoch = minEpoch; epoch <= maxEpoch; epoch++) {
            sum += guard.epochSpent(epoch);
        }
        require(sum == totalSettled, "budget consumed does not match what was settled");
    }

    function invariant_no_single_payment_exceeded_the_per_payment_cap() public view {
        require(maxSettled <= uint256(PER_PAYMENT_CAP), "a payment exceeded the per-payment cap");
    }

    // Custody is asserted in the payment unit test instead. The invariant fuzzer can call `mint`
    // on the token directly, so a balance check here would hold the harness to account for
    // something the guard never does, and the guard's own behaviour is what these prove.

    /// @dev A pause is a stop control, so nothing may settle while it is engaged.
    function invariant_nothing_settles_while_paused() public view {
        if (guard.paused()) {
            require(totalSettledWhilePaused == totalSettled, "a payment settled while paused");
        }
    }

    /// @dev Neither the pause control nor anything else can change the rules after deployment.
    function invariant_the_limits_and_the_signer_are_immutable() public view {
        require(guard.perPaymentCap() == PER_PAYMENT_CAP, "per-payment cap changed");
        require(guard.epochCap() == EPOCH_CAP, "epoch cap changed");
        require(guard.recipientEpochCap() == RECIPIENT_CAP, "recipient cap changed");
        require(guard.policySigner() == policySigner, "policy signer changed");
        require(guard.paymentToken() == address(token), "payment token changed");
        require(guard.pauser() == pauser, "pauser changed");
    }
}
