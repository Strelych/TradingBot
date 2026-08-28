#!/usr/bin/env python3
"""Тесты для функций управления рисками и размерами позиций"""

import sys
sys.path.insert(0, '/workspace')

def run_tests():
    # Импортируем модуль сервера для тестирования функций
    try:
        from server import eff_risk_mult, compute_size
        print('✅ Модуль server.py успешно импортирован')
    except Exception as e:
        print(f'❌ Ошибка импорта: {e}')
        return False

    # Тестовые данные - теперь передаем symbol и strat
    test_cases = [
        {'symbol': 'SOLUSDT', 'strat': 'test', 'canary': 0.0, 'expected_risk': 0.0, 'desc': 'Блокировка (canary=0)'},
        {'symbol': 'BTCUSDT', 'strat': 'test', 'canary': None, 'expected_risk': 1.0, 'desc': 'Полный риск (canary=None)'},
        {'symbol': 'ETHUSDT', 'strat': 'test', 'canary': 0.5, 'expected_risk': 0.5, 'desc': 'Сниженный риск (canary=0.5)'},
        {'symbol': 'BNBUSDT', 'strat': 'test', 'canary': 0.8, 'expected_risk': 0.8, 'desc': 'Сниженный риск (canary=0.8)'},
    ]

    print('\n🧪 Тестирование функции eff_risk_mult:')
    print('=' * 60)
    all_passed = True

    for case in test_cases:
        # Устанавливаем значение канарейки в адаптере
        from server import adapter
        adapter.adapter.canary[case['symbol']] = case['canary']
        
        result = eff_risk_mult(case['symbol'], case['strat'])
        status = '✅ PASS' if abs(result - case['expected_risk']) < 0.001 else '❌ FAIL'
        if status == '❌ FAIL': 
            all_passed = False
        print(f"{case['symbol']}: {case['desc']}")
        print(f"   Ожидается: {case['expected_risk']}, Получено: {result} -> {status}")

    print('\n🧪 Тестирование функции compute_size (симуляция):')
    print('=' * 60)

    # Тест случая с заблокированной торговлей (rm=0)
    try:
        # Симуляция: баланс 10000, риск 0 (заблокировано), цена 100
        size_blocked = compute_size('BTCUSDT', 100, 1.0, 0) 
        status_blocked = '✅ PASS' if size_blocked == 0.0 else '❌ FAIL'
        if status_blocked == '❌ FAIL': 
            all_passed = False
        print(f'Блокировка (risk=0): размер={size_blocked} -> {status_blocked}')
        
        # Симуляция: баланс 10000, риск 1.0 (полный), цена 100, мин.кол-во 0.01
        size_full = compute_size('BTCUSDT', 100, 1.0, 1.0)
        status_full = '✅ PASS' if size_full > 0 else '❌ FAIL'
        if status_full == '❌ FAIL': 
            all_passed = False
        print(f'Полный риск (risk=1.0): размер={size_full} -> {status_full}')
        
        # Симуляция: баланс 10000, риск 0.5 (половина), цена 100
        size_half = compute_size('BTCUSDT', 100, 1.0, 0.5)
        status_half = '✅ PASS' if size_half > 0 and size_half < size_full else '❌ FAIL'
        if status_half == '❌ FAIL': 
            all_passed = False
        print(f'Половинный риск (risk=0.5): размер={size_half} -> {status_half}')
        
    except Exception as e:
        print(f'❌ Ошибка при тестировании compute_size: {e}')
        import traceback
        traceback.print_exc()
        all_passed = False

    print('\n' + '=' * 60)
    if all_passed:
        print('🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО!')
        return True
    else:
        print('⚠️ НЕКОТОРЫЕ ТЕСТЫ ПРОВАЛЕНЫ! Требуется доработка.')
        return False

if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
