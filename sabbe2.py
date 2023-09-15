import random
import time
from pathlib import Path

from eth_account import Account
from eth_account.signers.local import LocalAccount

import constants
import enums
import utils
from logger import logging
from zksync2.core.types import EthBlockParams

from zksync2.module.module_builder import ZkSyncBuilder
from zksync2.manage_contracts.erc20_contract import ERC20Contract

ZERO_ADDRESS = '0x0000000000000000000000000000000000000000'

class ContractTypes(enums.AutoEnum):
    LIQUIDITY_MANAGER = enums.auto()
    SWAP = enums.auto()

CONTRACT_ADRESSES = {
    ContractTypes.LIQUIDITY_MANAGER: {
        enums.NetworkNames.zkEra: '0x936c9A1B8f88BFDbd5066ad08e5d773BC82EB15F',
        enums.NetworkNames.zkEraTestnet: '0x25727b360604E1e6B440c3B25aF368F54fc580B6',
    },
    ContractTypes.SWAP: {
        enums.NetworkNames.zkEra: '0x9606eC131EeC0F84c95D82c9a63959F2331cF2aC',
        enums.NetworkNames.zkEraTestnet: '0x3040EE148D09e5B92956a64CDC78b49f48C0cDdc',
    }
}


def swap(
    private_key: str,
    network_name: enums.NetworkNames,
    from_token_name: enums.TokenNames,
    to_token_name: enums.TokenNames,
    slippage: float,
    *,
    amount: float = None,
    percentage: float = None,
    proxy: dict[str, str] = None
):
    if not any([amount, percentage]):
        raise ValueError('Either amount or percentage must be specified')
    elif all([amount, percentage]):
        raise ValueError('Only one of amount or percentage must be specified')

    network = constants.NETWORKS[network_name]

    zk_web3 = ZkSyncBuilder.build(network.rpc_url, proxy=proxy)

    account: LocalAccount = Account.from_key(private_key)

    with open(Path(__file__).parent / 'abi' / 'liquidityManager.json') as file:
        liquidity_manager_abi = file.read()

    liquidity_manager_contract = zk_web3.eth.contract(
        address=CONTRACT_ADRESSES[ContractTypes.LIQUIDITY_MANAGER][network_name],
        abi=liquidity_manager_abi
    )

    weth_address = liquidity_manager_contract.functions.WETH9().call()
    weth_contract = ERC20Contract(zk_web3.zksync, weth_address, account)
    weth_decimals = weth_contract.contract.functions.decimals().call()

    if from_token_name in constants.ETH_TOKENS:
        from_token_address = weth_address
        from_token_contract = weth_contract
        from_token_decimals = weth_decimals
        balance_in_wei = zk_web3.zksync.get_balance(account.address)
    else:
        from_token = constants.NETWORK_TOKENS[network_name, from_token_name]
        from_token_address = from_token.contract_address
        from_token_contract = ERC20Contract(zk_web3.zksync, from_token_address, account)
        from_token_decimals = from_token.decimals
        balance_in_wei = from_token_contract.contract.functions.balanceOf(
            account.address
        ).call()

    if to_token_name in constants.ETH_TOKENS:
        to_token_address = weth_address
        to_token_contract = weth_contract
        to_token_decimals = weth_decimals
    else:
        to_token = constants.NETWORK_TOKENS[network_name, to_token_name]
        to_token_address = to_token.contract_address
        to_token_contract = ERC20Contract(zk_web3.zksync, to_token_address, account)
        to_token_decimals = to_token.decimals

    if amount is None:
        if percentage == 100:
            amount_in_wei = balance_in_wei
        else:
            amount_in_wei = int(balance_in_wei * percentage / 100)
        amount = amount_in_wei / 10 ** from_token_decimals
    else:
        amount_in_wei = int(amount * 10 ** from_token_decimals)

    logging.info(f'[iZUMi] Swapping {amount} {from_token_name} to {to_token_name}')

    fee = 0.2
    fee = int(fee * 10000)

    pool_address = liquidity_manager_contract.functions.pool(
        from_token_address, to_token_address, fee
    ).call()

    with open(Path(__file__).parent / 'abi' / 'pool.json') as file:
        pool_abi = file.read()

    pool_contract = zk_web3.eth.contract(
        address=pool_address,
        abi=pool_abi
    )

    try:
        state = pool_contract.functions.state().call()
    except Exception as e:
        logging.error(f'[iZUMi] Error getting pool state: {e}')
        return enums.TransactionStatus.FAILED

    with open(Path(__file__).parent / 'abi' / 'swap.json') as file:
        swap_abi = file.read()

    swap_contract = zk_web3.eth.contract(
        address=CONTRACT_ADRESSES[ContractTypes.SWAP][network_name],
        abi=swap_abi
    )

    token_x = from_token_address
    token_y = to_token_address

    txn_dict = {
        'nonce': zk_web3.zksync.get_transaction_count(account.address, EthBlockParams.LATEST.value),
        'maxPriorityFeePerGas': 100_000_000,
        'maxFeePerGas': zk_web3.zksync.gas_price,
        'gas': 0,
        'from': account.address
    }

    price_undecimal = 1.0001 ** state[1]

    if from_token_address.lower() < to_token_address.lower():
        boundary_pt = -799999
        func = swap_contract.functions.swapX2Y
    else:
        boundary_pt = 799999
        token_x, token_y = token_y, token_x
        func = swap_contract.functions.swapY2X
        price_undecimal = 1 / price_undecimal

    price = price_undecimal  * 10 ** from_token_decimals / 10 ** to_token_decimals

    min_amount_out = int(amount * price * 10 ** to_token_decimals * (1 - slippage / 100))

    swap_calling = func(list({
        'tokenX': token_x,
        'tokenY': token_y,
        'fee': fee,
        'boundaryPt': boundary_pt,
        'recipient': ZERO_ADDRESS if to_token_name in constants.ETH_TOKENS else account.address,
        'amount': amount_in_wei,
        'maxPayed': 0,
        'minAcquired': min_amount_out,
        'deadline': zk_web3.zksync.get_block('latest')['timestamp'] + 1800
    }.values()))

    callings = [swap_calling]

    if from_token_name in constants.ETH_TOKENS:
        callings.append(swap_contract.functions.refundETH())
        txn_dict['value'] = amount_in_wei
    else:
        allowance = from_token_contract.contract.functions.allowance(
            account.address, swap_contract.address
        ).call()

        if allowance < amount_in_wei:
            approve_amount_in_wei = amount_in_wei * 10
            approve_amount = amount * 10
            logging.info(f'[iZUMi] Approving {approve_amount} {from_token_name}')
            approve_txn = from_token_contract.contract.functions.approve(
                swap_contract.address,
                approve_amount_in_wei
            ).build_transaction(txn_dict)
            try:
                approve_txn['gas'] = zk_web3.zksync.eth_estimate_gas(approve_txn)
            except Exception as e:
                if 'insufficient balance' in str(e):
                    logging.critical(f'[iZUMi] Insufficient balance to approve {from_token_name}')
                    return enums.TransactionStatus.INSUFFICIENT_BALANCE
                logging.error(f'[iZUMi] Error while estimating gas: {e}')
                return enums.TransactionStatus.FAILED
            signed_approve = account.sign_transaction(approve_txn)
            approve_tx_hash = zk_web3.eth.send_raw_transaction(signed_approve.rawTransaction)
            logging.info(f'[iZUMi] Approve Transaction: {network.txn_explorer_url}{approve_tx_hash.hex()}')
            approve_receipt = utils.wait_for_transaction_receipt(
                web3=zk_web3.zksync,
                txn_hash=approve_tx_hash,
                logging_prefix='iZUMi'
            )

            if approve_receipt and approve_receipt['status'] == 1:
                logging.info(f'[iZUMi] Successfully approved {approve_amount} {from_token_name}')
            else:
                logging.error(f'[iZUMi] Failed to approve {approve_amount} {from_token_name}')
                return enums.TransactionStatus.FAILED
            txn_dict['nonce'] += 1

            while True:
                allowance = from_token_contract.contract.functions.allowance(
                    account.address, swap_contract.address
                ).call()
                if allowance >= amount_in_wei:
                    break
                time.sleep(5)

            utils.random_sleep()

    if to_token_name in constants.ETH_TOKENS:
        callings.append(swap_contract.functions.unwrapWETH9(0, account.address))
    if len(callings) == 1:
        func = callings[0]
    else:
        multicall = []
        for func in callings:
            multicall.append(swap_contract.encodeABI(
                fn_name=func.fn_name,
                args=func.args
            ))

        func = swap_contract.functions.multicall(multicall)

    txn = func.build_transaction(txn_dict)

    try:
        txn['gas'] = zk_web3.zksync.eth_estimate_gas(txn)
    except Exception as e:
        if 'insufficient balance' in str(e):
            logging.critical(f'[iZUMi] Insufficient balance to swap {from_token_name} to {to_token_name}')
            return enums.TransactionStatus.INSUFFICIENT_BALANCE
        logging.error(f'[iZUMi] Error while estimating gas: {e}')
        return enums.TransactionStatus.FAILED

    signed_txn = zk_web3.eth.account.sign_transaction(txn, private_key=private_key)

    txn_hash = zk_web3.eth.send_raw_transaction(signed_txn.rawTransaction)

    logging.info(f'[iZUMi] Transaction: {network.txn_explorer_url}{txn_hash.hex()}')

    receipt = utils.wait_for_transaction_receipt(
        web3=zk_web3.zksync,
        txn_hash=txn_hash,
        logging_prefix='iZUMi'
    )

    if receipt and receipt['status'] == 1:
        logging.info(f'[iZUMi] Successfully swapped {amount} {from_token_name} to {to_token_name}')
        return enums.TransactionStatus.SUCCESS
    else:
        logging.error(f'[iZUMi] Failed to swap {amount} {from_token_name} to {to_token_name}')
        return enums.TransactionStatus.FAILED


def add_liquidity(
    private_key: str,
    network_name: enums.NetworkNames,
    first_token_name: enums.TokenNames,
    second_token_name: enums.TokenNames,
    *,
    amount: float = None,
    percentage: float = None,
    proxy: dict[str, str] = None
):
    if not any([amount, percentage]):
        raise ValueError('Either amount or percentage must be specified')
    elif all([amount, percentage]):
        raise ValueError('Only one of amount or percentage must be specified')

    network = constants.NETWORKS[network_name]

    zk_web3 = ZkSyncBuilder.build(network.rpc_url, proxy=proxy)

    account: LocalAccount = Account.from_key(private_key)

    with open(Path(__file__).parent / 'abi' / 'liquidityManager.json') as file:
        liquidity_manager_abi = file.read()

    liquidity_manager_contract = zk_web3.eth.contract(
        address=CONTRACT_ADRESSES[ContractTypes.LIQUIDITY_MANAGER][network_name],
        abi=liquidity_manager_abi
    )

    weth_address = liquidity_manager_contract.functions.WETH9().call()
    weth_contract = ERC20Contract(zk_web3.zksync, weth_address, account)
    weth_decimals = weth_contract.contract.functions.decimals().call()

    if first_token_name in constants.ETH_TOKENS:
        first_token_address = weth_address
        first_token_contract = weth_contract
        first_token_decimals = weth_decimals
        first_balance_in_wei = zk_web3.zksync.get_balance(account.address)
    else:
        first_token = constants.NETWORK_TOKENS[network_name, first_token_name]
        first_token_address = first_token.contract_address
        first_token_contract = ERC20Contract(zk_web3.zksync, first_token_address, account)
        first_token_decimals = first_token.decimals
        first_balance_in_wei = first_token_contract.contract.functions.balanceOf(
            account.address
        ).call()

    if second_token_name in constants.ETH_TOKENS:
        second_token_address = weth_address
        second_token_contract = weth_contract
        second_token_decimals = weth_decimals
    else:
        second_token = constants.NETWORK_TOKENS[network_name, second_token_name]
        second_token_address = second_token.contract_address
        second_token_contract = ERC20Contract(zk_web3.zksync, second_token_address, account)
        second_token_decimals = second_token.decimals

    if amount is None:
        if percentage == 100:
            max_first_amount_in_wei = first_balance_in_wei
        else:
            max_first_amount_in_wei = int(first_balance_in_wei * percentage / 100)
        amount = max_first_amount_in_wei / 10 ** first_token_decimals
    else:
        max_first_amount_in_wei = int(amount * 10 ** first_token_decimals)

    logging.info(f'[iZUMi] Adding {amount} {first_token_name} to {first_token_name}/{second_token_name} liquidity pool')
    logging.info(f'[iZUMi] Swapping {amount / 2} {first_token_name} to {second_token_name} to add liquidity')

    swap_result = swap(
        private_key=private_key,
        network_name=network_name,
        from_token_name=first_token_name,
        to_token_name=second_token_name,
        slippage=0.5,
        amount=amount / 2
    )

    if swap_result != enums.TransactionStatus.SUCCESS:
        return swap_result

    utils.random_sleep()

    if first_token_name in constants.ETH_TOKENS:
        first_balance_in_wei = zk_web3.zksync.get_balance(account.address)
    else:
        first_balance_in_wei = first_token_contract.contract.functions.balanceOf(
            account.address
        ).call()

    if second_token_name in constants.ETH_TOKENS:
        second_balance_in_wei = zk_web3.zksync.get_balance(account.address)
    else:
        second_balance_in_wei = second_token_contract.contract.functions.balanceOf(
            account.address
        ).call()

    max_first_amount_in_wei = min(max_first_amount_in_wei // 2, first_balance_in_wei)
    max_second_amount_in_wei = second_balance_in_wei

    max_first_amount = max_first_amount_in_wei / 10 ** first_token_decimals
    max_second_amount = max_second_amount_in_wei / 10 ** second_token_decimals

    logging.info(f'[iZUMi] Adding {max_first_amount} {first_token_name} and {max_second_amount} {second_token_name} to liquidity pool')

    fee = 0.2
    fee = int(fee * 10000)

    pool_address = liquidity_manager_contract.functions.pool(
        first_token_address, second_token_address, fee
    ).call()

    pool_id = liquidity_manager_contract.functions.poolIds(
        pool_address
    ).call()

    liquidities = liquidity_manager_contract.functions.liquidities(
        pool_id
    ).call()

    with open(Path(__file__).parent / 'abi' / 'pool.json') as file:
        pool_abi = file.read()

    pool_contract = zk_web3.eth.contract(
        address=pool_address,
        abi=pool_abi
    )

    try:
        state = pool_contract.functions.state().call()
    except Exception as e:
        logging.error(f'[iZUMi] Error getting pool state: {e}')
        return enums.TransactionStatus.FAILED

    point1 = int(state[1] * random.uniform(0.5, 0.75))
    point2 = int(state[1] * random.uniform(1.5, 2))

    left_point = min(point1, point2)
    right_point = max(point1, point2)

    point_delta = pool_contract.functions.pointDelta().call()

    mod = left_point % point_delta
    if mod < point_delta / 2:
        left_point = left_point - mod
    else:
        left_point = left_point + point_delta - mod

    mod = right_point % point_delta
    if mod < point_delta / 2:
        right_point = right_point - mod
    else:
        right_point = right_point + point_delta - mod

    left_most_point = pool_contract.functions.leftMostPt().call()
    right_most_point = pool_contract.functions.rightMostPt().call()

    left_point = max(left_point, left_most_point)
    right_point = min(right_point, right_most_point)

    if first_token_address.lower() < second_token_address.lower():
        token_x = first_token_address
        token_y = second_token_address
    else:
        token_x = second_token_address
        token_y = first_token_address

    txn_data = {
        'nonce': zk_web3.zksync.get_transaction_count(account.address, EthBlockParams.LATEST.value),
        'maxPriorityFeePerGas': 100_000_000,
        'maxFeePerGas': zk_web3.zksync.gas_price,
        'gas': 0,
        'from': account.address
    }

    for token_name, token_contract, amount_in_wei in zip(
        [first_token_name, second_token_name],
        [first_token_contract, second_token_contract],
        [max_first_amount_in_wei, max_second_amount_in_wei]
    ):
        if token_name in constants.ETH_TOKENS:
            continue

        allowance = token_contract.contract.functions.allowance(
            account.address,
            liquidity_manager_contract.address
        ).call()

        if allowance < amount_in_wei:
            approve_amount_in_wei = amount_in_wei * 10
            approve_amount = approve_amount_in_wei / 10 ** token_contract.contract.functions.decimals().call()
            logging.info(f'[iZUMi] Approving {approve_amount} {token_name} to liquidity manager contract')
            approve_txn = token_contract.contract.functions.approve(
                liquidity_manager_contract.address,
                approve_amount_in_wei
            ).build_transaction(txn_data)
            try:
                approve_txn['gas'] = zk_web3.zksync.eth_estimate_gas(
                    approve_txn)
            except Exception as e:
                if 'insufficient balance' in str(e):
                    logging.critical(f'[iZUMi] Insufficient balance to approve {first_token_name}')
                    return enums.TransactionStatus.INSUFFICIENT_BALANCE
                logging.error(f'[iZUMi] Error while estimating gas: {e}')
                return enums.TransactionStatus.FAILED
            signed_approve = account.sign_transaction(approve_txn)
            approve_tx_hash = zk_web3.eth.send_raw_transaction(signed_approve.rawTransaction)
            logging.info(f'[iZUMi] Approve transaction: {network.txn_explorer_url}{approve_tx_hash.hex()}')
            approve_receipt = utils.wait_for_transaction_receipt(
                web3=zk_web3.zksync,
                txn_hash=approve_tx_hash,
                logging_prefix='iZUMi'
            )

            if approve_receipt and approve_receipt['status'] == 1:
                logging.info(f'[iZUMi] Successfully approved {approve_amount} liquidity tokens')
            else:
                logging.error(f'[iZUMi] Failed to approve {approve_amount} liquidity tokens')
                return enums.TransactionStatus.FAILED
            txn_data['nonce'] += 1

            while True:
                allowance = token_contract.contract.functions.allowance(
                    account.address, liquidity_manager_contract.address
                ).call()
                if allowance >= amount_in_wei:
                    break
                time.sleep(5)

            utils.random_sleep()


    mint_data = {
        'miner': account.address,
        'tokenX': token_x,
        'tokenY': token_y,
        'fee': fee,
        'pl': left_point,
        'pr': right_point,
        'xLim': max_first_amount_in_wei,
        'yLim': max_second_amount_in_wei,
        'amountXMin': 0,
        'amountYMin': 0,
        'deadline': zk_web3.zksync.get_block('latest')['timestamp'] + 1800
    }

    mint_calling = liquidity_manager_contract.functions.mint(list(mint_data.values()))

    callings = [mint_calling]

    if first_token_name in constants.ETH_TOKENS:
        txn_data['value'] = max_first_amount_in_wei
        callings.append(liquidity_manager_contract.functions.refundETH())
        multicall = []
        for calling in callings:
            multicall.append(liquidity_manager_contract.encodeABI(
                fn_name=calling.fn_name,
                args=calling.args
            ))
        func = liquidity_manager_contract.functions.multicall(multicall)
    else:
        func = mint_calling

    txn = func.build_transaction(txn_data)

    try:
        txn['gas'] = zk_web3.zksync.eth_estimate_gas(txn)
    except Exception as e:
        if 'insufficient balance' in str(e):
            logging.critical(f'[iZUMi] Insufficient balance to add liquidity')
            return enums.TransactionStatus.INSUFFICIENT_BALANCE
        logging.error(f'[iZUMi] Error while estimating gas: {e}')
        return enums.TransactionStatus.FAILED

    signed_txn = zk_web3.eth.account.sign_transaction(txn, private_key=private_key)

    txn_hash = zk_web3.eth.send_raw_transaction(signed_txn.rawTransaction)

    logging.info(f'[iZUMi] Transaction: {network.txn_explorer_url}{txn_hash.hex()}')

    receipt = utils.wait_for_transaction_receipt(
        web3=zk_web3.zksync,
        txn_hash=txn_hash,
        logging_prefix='iZUMi'
    )

    if receipt and receipt['status'] == 1:
        logging.info(f'[iZUMi] Successfully added liquidity to {first_token_name}/{second_token_name} pool')
        return enums.TransactionStatus.SUCCESS
    else:
        logging.error(f'[iZUMi] Failed to add liquidity to {first_token_name}/{second_token_name} pool')
        return enums.TransactionStatus.FAILED


def remove_random_liquidity(
    private_key: str,
    network_name: enums.NetworkNames,
    first_token_name: enums.TokenNames,
    second_token_name: enums.TokenNames,
    proxy: dict[str, str] = None
):
    network = constants.NETWORKS[network_name]

    zk_web3 = ZkSyncBuilder.build(network.rpc_url, proxy=proxy)

    account: LocalAccount = Account.from_key(private_key)

    with open(Path(__file__).parent / 'abi' / 'liquidityManager.json') as file:
        liquidity_manager_abi = file.read()

    liquidity_manager_contract = zk_web3.eth.contract(
        address=CONTRACT_ADRESSES[ContractTypes.LIQUIDITY_MANAGER][network_name],
        abi=liquidity_manager_abi
    )

    weth_address = liquidity_manager_contract.functions.WETH9().call()
    weth_contract = ERC20Contract(zk_web3.zksync, weth_address, account)
    weth_decimals = weth_contract.contract.functions.decimals().call()

    if first_token_name in constants.ETH_TOKENS:
        first_token_address = weth_address
        first_token_contract = weth_contract
        first_token_decimals = weth_decimals
    else:
        first_token = constants.NETWORK_TOKENS[network_name, first_token_name]
        first_token_address = first_token.contract_address
        first_token_contract = ERC20Contract(zk_web3.zksync, first_token_address, account)
        first_token_decimals = first_token.decimals

    if second_token_name in constants.ETH_TOKENS:
        second_token_address = weth_address
        second_token_contract = weth_contract
        second_token_decimals = weth_decimals
    else:
        second_token = constants.NETWORK_TOKENS[network_name, second_token_name]
        second_token_address = second_token.contract_address
        second_token_contract = ERC20Contract(zk_web3.zksync, second_token_address, account)
        second_token_decimals = second_token.decimals

    pool_address = liquidity_manager_contract.functions.pool(
        first_token_address, second_token_address, 2000
    ).call()

    pool_id = liquidity_manager_contract.functions.poolIds(
        pool_address
    ).call()

    total_liquidities = liquidity_manager_contract.functions.balanceOf(account.address).call()

    random_indexes = random.sample(range(total_liquidities), k=total_liquidities)

    liquidities = []

    logging.info(f'[iZUMi] Searching for liquidity in {first_token_name}/{second_token_name} pool')

    for i in random_indexes:
        token_id = liquidity_manager_contract.functions.tokenOfOwnerByIndex(account.address, i).call()
        liquidity = liquidity_manager_contract.functions.liquidities(token_id).call()
        if liquidity[-1] == pool_id and liquidity[2] > 0:
            liquidities.append((token_id, liquidity))
            break

    for token_id, liquidity in liquidities:
        logging.info(f'[iZUMi] Found liquidity in {first_token_name}/{second_token_name} pool, removing it')

        liquidity_amount = liquidity[2]

        is_chain_coin = bool({first_token_name, second_token_name}.intersection(constants.ETH_TOKENS))

        recipient = ZERO_ADDRESS if is_chain_coin else account.address

        callings = [
            liquidity_manager_contract.functions.decLiquidity(
                token_id,
                liquidity_amount,
                0,
                0,
                zk_web3.zksync.get_block('latest')['timestamp'] + 1800
            ),
            liquidity_manager_contract.functions.collect(
                recipient,
                token_id,
                2 ** 128 - 1,
                2 ** 128 - 1
            )
        ]

        if is_chain_coin:
            callings.append(liquidity_manager_contract.functions.unwrapWETH9(0, account.address))
            sweep_token_address = weth_address
            callings.append(liquidity_manager_contract.functions.sweepToken(weth_address, 0, account.address))


        multicall = []

        for call in callings:
            multicall.append(liquidity_manager_contract.encodeABI(
                fn_name=call.fn_name,
                args=call.args
            ))


        txn = liquidity_manager_contract.functions.multicall(multicall).build_transaction({
            'nonce': zk_web3.zksync.get_transaction_count(account.address, EthBlockParams.LATEST.value),
            'maxPriorityFeePerGas': 100_000_000,
            'maxFeePerGas': zk_web3.zksync.gas_price,
            'gas': 0,
            'from': account.address
        })

        try:
            txn['gas'] = zk_web3.eth.estimate_gas(txn)
        except Exception as e:
            if 'insufficient balance' in str(e):
                logging.critical(f'[iZUMi] Insufficient balance to remove liquidity from {first_token_name}/{second_token_name} pool')
                return enums.TransactionStatus.INSUFFICIENT_BALANCE
            logging.error(f'[iZUMi] Error while estimating gas: {e}')
            return enums.TransactionStatus.FAILED

        signed_txn = zk_web3.eth.account.sign_transaction(txn, private_key=private_key)

        txn_hash = zk_web3.eth.send_raw_transaction(signed_txn.rawTransaction)

        logging.info(f'[iZUMi] Transaction: {network.txn_explorer_url}{txn_hash.hex()}')

        receipt = utils.wait_for_transaction_receipt(
            web3=zk_web3.zksync,
            txn_hash=txn_hash,
            logging_prefix='iZUMi'
        )

        if receipt and receipt['status'] == 1:
            logging.info(f'[iZUMi] Successfully removed liquidity from {first_token_name}/{second_token_name} pool')
            return enums.TransactionStatus.SUCCESS
        else:
            logging.error(f'[iZUMi] Failed to remove liquidity from {first_token_name}/{second_token_name} pool')
            return enums.TransactionStatus.FAILED

    logging.warning(f'[iZUMi] No liquidity found in {first_token_name}/{second_token_name} pool')

    return enums.TransactionStatus.NO_LIQUIDITIES


def burn_random_liquidity(
    private_key: str,
    network_name: enums.NetworkNames,
    first_token_name: enums.TokenNames,
    second_token_name: enums.TokenNames,
    proxy: dict[str, str] = None
):
    network = constants.NETWORKS[network_name]

    zk_web3 = ZkSyncBuilder.build(network.rpc_url, proxy=proxy)

    account: LocalAccount = Account.from_key(private_key)

    with open(Path(__file__).parent / 'abi' / 'liquidityManager.json') as file:
        liquidity_manager_abi = file.read()

    liquidity_manager_contract = zk_web3.eth.contract(
        address=CONTRACT_ADRESSES[ContractTypes.LIQUIDITY_MANAGER][network_name],
        abi=liquidity_manager_abi
    )

    weth_address = liquidity_manager_contract.functions.WETH9().call()
    weth_contract = ERC20Contract(zk_web3.zksync, weth_address, account)
    weth_decimals = weth_contract.contract.functions.decimals().call()

    if first_token_name in constants.ETH_TOKENS:
        first_token_address = weth_address
        first_token_contract = weth_contract
        first_token_decimals = weth_decimals
    else:
        first_token = constants.NETWORK_TOKENS[network_name, first_token_name]
        first_token_address = first_token.contract_address
        first_token_contract = ERC20Contract(zk_web3.zksync, first_token_address, account)
        first_token_decimals = first_token.decimals

    if second_token_name in constants.ETH_TOKENS:
        second_token_address = weth_address
        second_token_contract = weth_contract
        second_token_decimals = weth_decimals
    else:
        second_token = constants.NETWORK_TOKENS[network_name, second_token_name]
        second_token_address = second_token.contract_address
        second_token_contract = ERC20Contract(zk_web3.zksync, second_token_address, account)
        second_token_decimals = second_token.decimals

    pool_address = liquidity_manager_contract.functions.pool(
        first_token_address, second_token_address, 2000
    ).call()

    pool_id = liquidity_manager_contract.functions.poolIds(
        pool_address
    ).call()

    total_liquidities = liquidity_manager_contract.functions.balanceOf(account.address).call()

    random_indexes = random.sample(range(total_liquidities), k=total_liquidities)

    liquidities = []

    logging.info(f'[iZUMi] Searching for positions in {first_token_name}/{second_token_name} pool to burn')

    for i in random_indexes:
        token_id = liquidity_manager_contract.functions.tokenOfOwnerByIndex(account.address, i).call()
        liquidity = liquidity_manager_contract.functions.liquidities(token_id).call()
        if liquidity[-1] == pool_id and liquidity[2] == 0 and liquidity[5] == 0 and liquidity[6] == 0:
            liquidities.append((token_id, liquidity))
            break

    for token_id, liquidity in liquidities:
        logging.info(f'[iZUMi] Found position in {first_token_name}/{second_token_name} pool')
        txn = liquidity_manager_contract.functions.burn(
            token_id
        ).build_transaction({
            'nonce': zk_web3.zksync.get_transaction_count(account.address, EthBlockParams.LATEST.value),
            'maxPriorityFeePerGas': 100_000_000,
            'maxFeePerGas': zk_web3.zksync.gas_price,
            'gas': 0,
            'from': account.address
        })

        try:
            txn['gas'] = zk_web3.eth.estimate_gas(txn)
        except Exception as e:
            if 'insufficient balance' in str(e):
                logging.critical(f'[iZUMi] Insufficient balance to remove liquidity from {first_token_name}/{second_token_name} pool')
                return enums.TransactionStatus.INSUFFICIENT_BALANCE
            logging.error(f'[iZUMi] Error while estimating gas: {e}')
            return enums.TransactionStatus.FAILED

        signed_txn = zk_web3.eth.account.sign_transaction(txn, private_key=private_key)

        txn_hash = zk_web3.eth.send_raw_transaction(signed_txn.rawTransaction)

        logging.info(f'[iZUMi] Transaction: {network.txn_explorer_url}{txn_hash.hex()}')

        receipt = utils.wait_for_transaction_receipt(
            web3=zk_web3.zksync,
            txn_hash=txn_hash,
            logging_prefix='iZUMi'
        )

        if receipt and receipt['status'] == 1:
            logging.info(f'[iZUMi] Successfully burned liquidity from {first_token_name}/{second_token_name} pool')
            return enums.TransactionStatus.SUCCESS
        else:
            logging.error(f'[iZUMi] Failed to burn liquidity from {first_token_name}/{second_token_name} pool')
            return enums.TransactionStatus.FAILED

    logging.warning(f'[iZUMi] No liquidity positions found in {first_token_name}/{second_token_name} pool')

    return enums.TransactionStatus.NO_LIQUIDITIES
