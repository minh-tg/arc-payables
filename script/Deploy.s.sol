// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {Counter} from "../src/Counter.sol";

contract DeployCounter {
    function run() external returns (Counter counter) {
        counter = new Counter();
    }
}
