class StaticArray {
    insertEnd(arr, n, length, capacity) {
        if (length < capacity) {
            arr[length] = n
        }
    }
    removeEnd(arr, length) {
        if (length > 0) {
            arr[length - 1] = 0;
        }
    }

    insertMiddle(arr, i, n, length) {
        for (let index = length - 1; index >= i; index--) {
            //assuming capacity is larger then length
            arr[index + 1] = arr[index]
        }
        arr[i] = n
    }

    removeMiddle(arr, i, length) {
        for (let index = i + 1; index < length; index++) {
            arr[index - 1] = arr[index];
        }
    }

    printArr(arr, length) {
        let s = "";
        for (let i = 0; i < length; i++) {
            s += arr[i] + " ";
        }
        console.log(s);

    }
}