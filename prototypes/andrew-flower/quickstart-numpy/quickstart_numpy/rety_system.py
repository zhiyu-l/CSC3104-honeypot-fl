max_retries = 2
required_updates = 2

successful_updates = [1, 1, 3]

for attempt in range(max_retries + 1):

    valid = successful_updates[attempt]

    print(f"Attempt {attempt + 1}: {valid} valid updates")

    if valid >= required_updates:
        print("Quorum reached.")
        break
    else:
        print("Quorum not reached.")

        if attempt == 2:
            print("Retries exhausted.")

    # Your code here