// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {Counter} from "../src/Counter.sol";

contract CounterTest {
    Counter public counter;

    function setUp() public {
        counter = new Counter();
        counter.setNumber(0);
    }

    function test_Increment() public {
        counter.increment();
        require(counter.number() == 1, "Counter should be 1");
    }

    function test_SetNumber(uint256 x) public {
        counter.setNumber(x);
        require(counter.number() == x, "Counter value mismatch");
    }
}
