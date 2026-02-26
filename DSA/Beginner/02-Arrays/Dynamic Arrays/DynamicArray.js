class DynamicArray {
    constructor() {
        this.capacity = 2;
        this.length = 0;
        this.arr = new Array(2);
    }

    pushback(n) {
        if (this.length == this.capacity) {
            this.resize();
        }
        this.arr[this.length] = n;
        this.length++;
    }

    resize() {
        this.capacity = 2 * this.capacity;
        const newArr = new Array(this.capacity);
        for (let i = 0; i < this.length; i++) {
            newArr[i] = this.arr[i];
        }
        this.arr = newArr;
    }

    popback() {
        if (this.length > 0) {
            this.length--;
        }
    }

    get(i) {
        if (i < this.length) {
            return this.arr[i];
        }
        return undefined;
    }

    insert(i, n) {
        if (i < this.length) {
            this.arr[i] = n;
        }
    }

    print() {
        let s = ""; 
        for (let i = 0; i<this.length; i++) {
            s += arr[i] + " ";
        }
        console.log(s);
    }
}