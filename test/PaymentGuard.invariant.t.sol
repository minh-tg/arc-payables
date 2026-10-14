// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {PaymentGuard} from "../src/PaymentGuard.sol";
import {MockArcUSDC} from "./PaymentGuard.t.sol";

/// @notice The cheatcodes this file needs. No forge-std dependency.
interface VmInvariant {
    function warp(uint256 timestamp) external;
    function chainId(uint256 newChainId) external;
    function prank(address sender) external;
    function addr(uint256 privateKey) external returns (address);
    function sign(uint256 privateKey, bytes32 digest) external returns (uint8 v, bytes32 r, bytes32 s);
    function getBlockTimestamp() external view returns (uint256);
}

/// @notice The only fuzz target: funded, policy-signed payments and real time/pause transitions.
/// @dev Expected guard rejections are caught, but successful effects are always counted, including
///      an illegal success while paused. Never overwrite that evidence with the post-call total.
contract PaymentGuardHandler {
    VmInvariant private constant vm = VmInvariant(address(uint160(uint256(keccak256("hevm cheat code")))));
    uint96 private constant PAYMENT_LIMIT = 100e6;
    uint256 private immutable policyKey;
    PaymentGuard private immutable guard;
    address private immutable pauser;

    address[3] public recipients = [address(0xBEEF), address(0xF00D), address(0xCAFE)];
    uint64 public minEpoch;
    uint64 public maxEpoch;
    uint256 public totalSettled;
    uint256 public maxSettled;
    uint256 public settledWhilePaused;
    uint256 public actionCalls;
    uint256 public paymentAttempts;
    uint256 public successfulPayments;
    uint256 public pausedAttempts;
    uint256 public timeMoves;
    uint256 public pauseTransitions;
    uint256 public unpauseTransitions;

    constructor(MockArcUSDC token, PaymentGuard guard_, uint256 policyKey_, address pauser_) {
        guard = guard_;
        policyKey = policyKey_;
        pauser = pauser_;
        token.mint(address(this), 1_000_000e6);
        token.approve(address(guard), type(uint256).max);
        minEpoch = guard.epochAt(block.timestamp);
        maxEpoch = minEpoch;
    }

    function pay(uint96 amountSeed, uint8 recipientIndex, uint64 expiryOffset, bytes32 paymentId) public {
        actionCalls++;
        paymentAttempts++;
        // Structured inputs reach successful spends as well as zero/over-cap/replay rejection.
        // Raw uint96 amounts otherwise overwhelmingly exceed a 100-USDC cap.
        uint256 amount = uint256(amountSeed) % (2 * uint256(PAYMENT_LIMIT) + 1);
        address recipient = recipients[recipientIndex % 3];
        PaymentGuard.Permit memory permit = PaymentGuard.Permit({
            payer: address(this),
            token: guard.paymentToken(),
            recipient: recipient,
            amount: amount,
            evidenceHash: keccak256(abi.encode(paymentId, recipient)),
            paymentId: paymentId,
            expiry: uint64(block.timestamp + 1 + (expiryOffset % 3600))
        });
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(policyKey, guard.hashPermit(permit));
        bool wasPaused = guard.paused();
        if (wasPaused) pausedAttempts++;
        try guard.pay(permit, abi.encodePacked(r, s, v)) {
            successfulPayments++;
            totalSettled += amount;
            if (amount > maxSettled) maxSettled = amount;
            if (wasPaused) settledWhilePaused += amount;
            _trackEpoch();
        } catch {}
    }

    function moveTime(uint64 secondsToMove) public {
        actionCalls++;
        timeMoves++;
        // Include epoch crossings, rather than only sub-epoch time movements.
        vm.warp(vm.getBlockTimestamp() + 1 + (secondsToMove % 7200));
        _trackEpoch();
    }

    function pauseGuard() public {
        actionCalls++;
        vm.prank(pauser);
        try guard.pause() {
            pauseTransitions++;
        } catch {}
    }

    function unpauseGuard() public {
        actionCalls++;
        vm.prank(pauser);
        try guard.unpause() {
            unpauseTransitions++;
        } catch {}
    }

    function _trackEpoch() private {
        uint64 epoch = guard.epochAt(block.timestamp);
        if (epoch < minEpoch) minEpoch = epoch;
        if (epoch > maxEpoch) maxEpoch = epoch;
    }
}

/// @notice Stateful checks plus deterministic, non-vacuous regressions for the fuzz harness.
contract PaymentGuardInvariantTest {
    VmInvariant private constant vm = VmInvariant(address(uint160(uint256(keccak256("hevm cheat code")))));
    uint256 private constant POLICY_KEY = 0xA11CE;
    uint256 private constant CHAIN_ID = 5_042_002;
    uint96 private constant PER_PAYMENT_CAP = 100e6;
    uint96 private constant EPOCH_CAP = 1_000e6;
    uint96 private constant RECIPIENT_CAP = 500e6;
    uint64 private constant EPOCH_LENGTH = 3_600;

    MockArcUSDC private token;
    PaymentGuard private guard;
    PaymentGuardHandler private handler;
    address private pauser = address(0x5a5);
    address private policySigner;

    // These ABI getters are Foundry's invariant-target protocol, not Vm cheatcodes.
    struct FuzzSelector {
        address addr;
        bytes4[] selectors;
    }

    function targetContracts() public view returns (address[] memory targets) {
        targets = new address[](1);
        targets[0] = address(handler);
    }

    function targetSelectors() public view returns (FuzzSelector[] memory targets) {
        bytes4[] memory selectors = new bytes4[](4);
        selectors[0] = PaymentGuardHandler.pay.selector;
        selectors[1] = PaymentGuardHandler.moveTime.selector;
        selectors[2] = PaymentGuardHandler.pauseGuard.selector;
        selectors[3] = PaymentGuardHandler.unpauseGuard.selector;
        targets = new FuzzSelector[](1);
        targets[0] = FuzzSelector(address(handler), selectors);
    }

    function setUp() public {
        vm.chainId(CHAIN_ID);
        token = new MockArcUSDC();
        policySigner = vm.addr(POLICY_KEY);
        guard = new PaymentGuard(
            address(token), policySigner, pauser, PER_PAYMENT_CAP, EPOCH_CAP, RECIPIENT_CAP, EPOCH_LENGTH
        );
        handler = new PaymentGuardHandler(token, guard, POLICY_KEY, pauser);
    }

    /// @dev Fail if target selection silently stops exercising the handler. Per-action coverage
    ///      is deterministic below, not a flaky requirement that every random sequence hit all four.
    function afterInvariant() public view {
        require(handler.actionCalls() > 0, "fuzzer did not exercise the handler");
    }

    function test_handler_exercises_settlement_replay_caps_pause_and_epoch_rollover() public {
        handler.pay(PER_PAYMENT_CAP, 0, 0, bytes32(uint256(1)));
        require(handler.successfulPayments() == 1, "valid handler payment never settled");
        require(token.balanceOf(handler.recipients(0)) == PER_PAYMENT_CAP, "supplier did not receive payment");
        handler.pay(PER_PAYMENT_CAP, 0, 0, bytes32(uint256(1)));
        handler.pay(PER_PAYMENT_CAP + 1, 0, 0, bytes32(uint256(2)));
        require(handler.successfulPayments() == 1, "replay or over-cap payment settled");

        handler.pauseGuard();
        require(guard.paused(), "handler did not pause");
        handler.pay(1, 1, 0, bytes32(uint256(3)));
        invariant_nothing_settles_while_paused();
        require(handler.successfulPayments() == 1 && handler.pausedAttempts() == 1, "paused attempt missing");
        handler.unpauseGuard();
        handler.pay(1, 1, 0, bytes32(uint256(3)));
        require(handler.successfulPayments() == 2, "unpause did not restore payment");

        uint64 epoch = guard.epochAt(block.timestamp);
        handler.moveTime(EPOCH_LENGTH);
        require(guard.epochAt(block.timestamp) > epoch, "time action did not cross an epoch");
        handler.pay(PER_PAYMENT_CAP, 0, 0, bytes32(uint256(4)));
        require(handler.successfulPayments() == 3, "new-epoch payment failed");
        require(
            handler.timeMoves() == 1 && handler.pauseTransitions() == 1 && handler.unpauseTransitions() == 1,
            "transition coverage missing"
        );
        invariant_epoch_spends_equal_what_was_actually_settled();
        invariant_token_balances_match_observed_settlements();
    }

    function test_handler_reaches_recipient_and_epoch_cap_boundaries() public {
        uint256 id = 1;
        for (uint8 recipient = 0; recipient < 2; recipient++) {
            for (uint256 count = 0; count < 5; count++) {
                handler.pay(PER_PAYMENT_CAP, recipient, 0, bytes32(id++));
            }
            if (recipient == 0) {
                handler.pay(1, recipient, 0, bytes32(id++));
                require(handler.totalSettled() == RECIPIENT_CAP, "recipient cap allowed one extra unit");
            }
        }
        require(handler.totalSettled() == EPOCH_CAP, "exact epoch cap did not settle");
        handler.pay(1, 2, 0, bytes32(id++));
        require(handler.totalSettled() == EPOCH_CAP, "epoch cap allowed one extra unit");
        handler.moveTime(EPOCH_LENGTH);
        handler.pay(1, 0, 0, bytes32(id));
        require(handler.totalSettled() == uint256(EPOCH_CAP) + 1, "rollover did not restore recipient budget");
        invariant_epoch_spend_never_exceeds_the_epoch_cap();
        invariant_recipient_spend_never_exceeds_its_cap();
    }

    function invariant_epoch_spend_never_exceeds_the_epoch_cap() public view {
        for (uint64 epoch = handler.minEpoch(); epoch <= handler.maxEpoch(); epoch++) {
            require(guard.epochSpent(epoch) <= uint256(EPOCH_CAP), "epoch spend exceeded the cap");
        }
    }

    function invariant_recipient_spend_never_exceeds_its_cap() public view {
        for (uint64 epoch = handler.minEpoch(); epoch <= handler.maxEpoch(); epoch++) {
            for (uint256 index = 0; index < 3; index++) {
                require(
                    guard.recipientEpochSpent(epoch, handler.recipients(index)) <= uint256(RECIPIENT_CAP),
                    "recipient spend exceeded its cap"
                );
            }
        }
    }

    function invariant_recipient_spends_sum_to_the_epoch_spend() public view {
        for (uint64 epoch = handler.minEpoch(); epoch <= handler.maxEpoch(); epoch++) {
            uint256 sum;
            for (uint256 index = 0; index < 3; index++) {
                sum += guard.recipientEpochSpent(epoch, handler.recipients(index));
            }
            require(sum == guard.epochSpent(epoch), "recipient spends do not sum to the epoch spend");
        }
    }

    function invariant_epoch_spends_equal_what_was_actually_settled() public view {
        uint256 sum;
        for (uint64 epoch = handler.minEpoch(); epoch <= handler.maxEpoch(); epoch++) {
            sum += guard.epochSpent(epoch);
        }
        require(sum == handler.totalSettled(), "budget consumed does not match what was settled");
    }

    function invariant_no_single_payment_exceeded_the_per_payment_cap() public view {
        require(handler.maxSettled() <= uint256(PER_PAYMENT_CAP), "a payment exceeded the per-payment cap");
    }

    function invariant_nothing_settles_while_paused() public view {
        require(handler.settledWhilePaused() == 0, "a payment settled while paused");
    }

    /// @dev Direct token mint/transfer actions are excluded, so balances independently measure
    ///      the handler's observations rather than trusting the guard's own budget bookkeeping.
    function invariant_token_balances_match_observed_settlements() public view {
        uint256 received;
        for (uint256 index = 0; index < 3; index++) {
            received += token.balanceOf(handler.recipients(index));
        }
        require(received == handler.totalSettled(), "supplier balances differ from observed settlements");
        require(token.balanceOf(address(handler)) + received == 1_000_000e6, "payer balance drifted");
    }

    function invariant_the_limits_and_the_signer_are_immutable() public view {
        require(guard.perPaymentCap() == PER_PAYMENT_CAP, "per-payment cap changed");
        require(guard.epochCap() == EPOCH_CAP, "epoch cap changed");
        require(guard.recipientEpochCap() == RECIPIENT_CAP, "recipient cap changed");
        require(guard.policySigner() == policySigner, "policy signer changed");
        require(guard.paymentToken() == address(token), "payment token changed");
        require(guard.pauser() == pauser, "pauser changed");
    }
}
